"""Tests for the one-way mirror sync (Xalt -> MoreValue CalDAV calendar).

Focus, in this order:

1. the hard invariant — the add-on only ever writes to or deletes its OWN,
   marked resources inside the configured collection,
2. changes and deletions, which is what Roland's previous external tool got
   wrong (time shifts, title changes, vanished appointments),
3. the window boundaries, orphan cleanup, conflict (412) handling and error
   isolation.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import settings
from app.caldav_write import build_ical, resource_name, resource_url
from app.mirror_sync import (
    MAX_CLEANUP_ATTEMPTS,
    MIRROR_SYNC_FUTURE_DAYS,
    mirror_sync_window,
    run_mirror_sync,
)
from app.models import CalendarEvent, MirrorEvent
from app.sources import google
from app.sources.google import save_tokens
from app.storage import Storage
from app.sync_identity import source_key

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

COLLECTION = "https://cloud.example.com/remote.php/dav/calendars/roland/mv/"
TARGET_CONFIG = {
    "url": "https://cloud.example.com",
    "username": "roland",
    "app_password": "secret",
    "calendar_url": COLLECTION,
}

# A second Nextcloud calendar — Roland created "XALT Termine" and wants the
# copies MOVED there, not duplicated.
OTHER_COLLECTION = "https://cloud.example.com/remote.php/dav/calendars/roland/xalt/"
OTHER_CONFIG = {**TARGET_CONFIG, "calendar_url": OTHER_COLLECTION}


def make_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


class RecordingCaldav:
    """Mock Nextcloud modelling one calendar collection.

    Holds foreign (unmarked) resources next to the add-on's own copies and
    raises the moment any request targets a foreign resource — that is what
    turns the invariant into an assertion instead of a comment.
    """

    def __init__(
        self,
        foreign: dict[str, str] | None = None,
        *,
        collection: str = COLLECTION,
    ) -> None:
        self.collection = collection
        # url -> (etag, ics text) for the add-on's own copies
        self.own: dict[str, tuple[str, str]] = {}
        # url -> ics text for real Nextcloud appointments
        self.foreign: dict[str, str] = dict(foreign or {})
        self.requests: list[httpx.Request] = []
        self.conflict_urls: set[str] = set()
        self.report_status = 207
        self._etag = 0

    def seed_own(self, key: str, event: CalendarEvent, *, url: str | None = None) -> str:
        target = url or resource_url(self.collection, key)
        self._etag += 1
        self.own[target] = (f'"e{self._etag}"', build_ical(key, event).decode())
        return target

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.foreign:
            raise AssertionError(f"foreign resource {url} was targeted!")
        if request.method == "REPORT":
            return self._report()
        if url in self.conflict_urls:
            return httpx.Response(412)
        if request.method == "PUT":
            return self._put(request, url)
        if request.method == "DELETE":
            return self._delete(url)
        return httpx.Response(405)

    def _report(self) -> httpx.Response:
        if self.report_status != 207:
            return httpx.Response(self.report_status)
        entries = [(url, etag, ics) for url, (etag, ics) in self.own.items()]
        entries += [(url, '"f"', ics) for url, ics in self.foreign.items()]
        responses = "".join(
            f"<d:response><d:href>{escape(url)}</d:href>"
            f"<d:propstat><d:prop><d:getetag>{escape(etag)}</d:getetag>"
            f"<cal:calendar-data>{escape(ics)}</cal:calendar-data></d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            for url, etag, ics in entries
        )
        body = (
            '<?xml version="1.0"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
            f"{responses}</d:multistatus>"
        )
        return httpx.Response(207, content=body.encode())

    def _put(self, request: httpx.Request, url: str) -> httpx.Response:
        if request.headers.get("If-None-Match") == "*" and url in self.own:
            return httpx.Response(412)
        self._etag += 1
        etag = f'"e{self._etag}"'
        self.own[url] = (etag, request.content.decode())
        return httpx.Response(204, headers={"ETag": etag})

    def _delete(self, url: str) -> httpx.Response:
        if url not in self.own:
            return httpx.Response(404)
        del self.own[url]
        return httpx.Response(204)

    def summaries(self) -> set[str]:
        return {
            line.split(":", 1)[1].strip()
            for _, ics in self.own.values()
            for line in ics.splitlines()
            if line.startswith("SUMMARY:")
        }


class CaldavCluster:
    """Routes mock requests to the collection they address.

    Needed as soon as two calendars are in play (target switch): the old
    target must be cleaned up while the new one receives the copies.
    """

    def __init__(self, *backends: RecordingCaldav) -> None:
        self.backends = backends

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for backend in self.backends:
            if url.startswith(backend.collection):
                return backend.handler(request)
        raise AssertionError(f"request outside every known collection: {url}")


def add_target_source(storage: Storage) -> int:
    return storage.add_source(type="caldav", name="Roland MV", config=TARGET_CONFIG)


def add_other_target_source(storage: Storage) -> int:
    return storage.add_source(
        type="caldav", name="XALT Termine", config=OTHER_CONFIG
    )


def add_xalt_source(storage: Storage) -> int:
    return storage.add_source(type="google", name="Roland@Xalt", config={})


def store_events(storage: Storage, source_id: int, events: list[CalendarEvent]) -> None:
    storage.sync_events(
        source_id,
        events,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2028, 1, 1, tzinfo=UTC),
        synced_at=NOW,
    )


def meeting(
    uid: str = "u1",
    *,
    title: str = "Kundentermin",
    start: datetime | None = None,
    hours: int = 1,
    location: str | None = None,
    description: str | None = None,
    organizer: str | None = None,
    attendees: str | None = None,
) -> CalendarEvent:
    begin = start or datetime(2026, 7, 20, 8, 15, tzinfo=UTC)
    return CalendarEvent(
        uid=uid,
        title=title,
        start=begin,
        end=begin + timedelta(hours=hours),
        all_day=False,
        location=location,
        description=description,
        organizer=organizer,
        attendees=attendees,
    )


@pytest.fixture
def env(tmp_path: Path) -> tuple[Storage, int, int]:
    """Storage with a Xalt source, a CalDAV target and the mirror enabled."""
    storage = make_storage(tmp_path)
    target_id = add_target_source(storage)
    xalt_id = add_xalt_source(storage)
    settings.set_mirror_sync_enabled(storage, True)
    settings.set_mirror_sync_source_ids(storage, [xalt_id])
    settings.set_mirror_sync_target_source_id(storage, target_id)
    return storage, xalt_id, target_id


async def _run(
    storage: Storage,
    backend: RecordingCaldav,
    *,
    source_results: dict[int, str | None] | None = None,
):
    """One mirror run.

    ``source_results`` is what the calendar sync reports about this very run
    (source id -> error or None). Omitting it means "no information", which is
    exactly the situation in which the data-loss guard refuses to delete.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handler)) as http:
        return await run_mirror_sync(
            storage, now=NOW, client=http, source_results=source_results
        )


class TestWindow:
    def test_window_is_180_days_from_today(self) -> None:
        start, end = mirror_sync_window(NOW)
        assert start == datetime(2026, 7, 9, tzinfo=BERLIN)
        assert (end - start).days == MIRROR_SYNC_FUTURE_DAYS


@pytest.mark.anyio
class TestContactSourcesAreNotMirrored:
    async def test_birthdays_of_an_unselected_contact_source_stay_out(
        self, env
    ) -> None:
        """Contact sources now fill the events table for a whole year.

        The mirror only ever writes the sources Roland selected, so the extra
        birthday rows must not leak into MoreValue — not even the ones inside
        the mirror's own 180-day window.
        """
        storage, xalt_id, _ = env
        contacts_id = storage.add_source(
            type="google_contacts", name="Geburtstage", config={}
        )
        store_events(storage, xalt_id, [meeting()])
        store_events(
            storage,
            contacts_id,
            [
                CalendarEvent(
                    uid="people/c1|2026",
                    title="🎂 Oma",
                    start=date(2026, 8, 20),
                    end=date(2026, 8, 21),
                    all_day=True,
                )
            ],
        )
        backend = RecordingCaldav()

        result = await _run(storage, backend, source_results={xalt_id: None})

        assert result.inserted == 1
        assert backend.summaries() == {"Kundentermin"}


@pytest.mark.anyio
class TestDisabledOrUnconfigured:
    async def test_disabled_does_nothing(self, env) -> None:
        storage, xalt_id, _ = env
        settings.set_mirror_sync_enabled(storage, False)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 0
        assert backend.requests == []

    async def test_without_target_does_nothing(self, env) -> None:
        storage, xalt_id, _ = env
        settings.set_mirror_sync_target_source_id(storage, None)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 0
        assert backend.requests == []

    async def test_non_caldav_target_does_nothing(self, env) -> None:
        storage, xalt_id, _ = env
        settings.set_mirror_sync_target_source_id(storage, xalt_id)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 0
        assert backend.requests == []


@pytest.mark.anyio
class TestInsert:
    async def test_creates_copy_with_the_real_title(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting(location="Raum 2")])
        backend = RecordingCaldav()
        result = await _run(storage, backend)

        assert result.inserted == 1
        assert storage.count_mirror_events() == 1
        (etag, ics), = list(backend.own.values())
        assert "SUMMARY:Kundentermin" in ics
        assert "LOCATION:Raum 2" in ics
        assert storage.list_mirror_events()[0].etag == etag

    async def test_daytime_meeting_is_mirrored_too(self, env) -> None:
        # Roland's trigger: a plain 10:15 meeting that the family filter
        # would hide must still reach MoreValue.
        storage, xalt_id, _ = env
        store_events(
            storage,
            xalt_id,
            [meeting(start=datetime(2026, 7, 20, 8, 15, tzinfo=UTC), title="10:15 Jour Fixe")],
        )
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 1
        assert backend.summaries() == {"10:15 Jour Fixe"}

    async def test_all_day_event_is_mirrored_as_date_range(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(
            storage,
            xalt_id,
            [
                CalendarEvent(
                    uid="d1",
                    title="Betriebsausflug",
                    start=date(2026, 7, 20),
                    end=date(2026, 7, 21),
                    all_day=True,
                )
            ],
        )
        backend = RecordingCaldav()
        await _run(storage, backend)
        (_, ics), = list(backend.own.values())
        assert "DTSTART;VALUE=DATE:20260720" in ics
        assert "DTEND;VALUE=DATE:20260721" in ics

    async def test_second_run_is_a_nulldiff(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        result = await _run(storage, backend)
        assert (result.inserted, result.updated, result.deleted) == (0, 0, 0)
        assert result.orphans_removed == 0

    async def test_only_selected_sources_are_mirrored(self, env) -> None:
        storage, xalt_id, _ = env
        other = storage.add_source(type="google", name="Privat", config={})
        store_events(storage, xalt_id, [meeting(uid="a")])
        store_events(storage, other, [meeting(uid="b", title="Privater Termin")])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 1
        assert backend.summaries() == {"Kundentermin"}


@pytest.mark.anyio
class TestUpdate:
    async def test_title_change_updates_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(title="Kundentermin (verschoben)")])
        result = await _run(storage, backend)

        assert result.updated == 1
        assert result.inserted == 0
        assert backend.summaries() == {"Kundentermin (verschoben)"}

    async def test_end_time_change_updates_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(hours=3)])
        result = await _run(storage, backend)

        assert result.updated == 1
        (_, ics), = list(backend.own.values())
        assert "DTEND:20260720T111500Z" in ics

    async def test_location_change_updates_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting(location="Raum 1")])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(location="Raum 9")])
        result = await _run(storage, backend)

        assert result.updated == 1
        (_, ics), = list(backend.own.values())
        assert "LOCATION:Raum 9" in ics

    async def test_update_sends_if_match_with_the_current_etag(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        current_etag = next(iter(backend.own.values()))[0]

        store_events(storage, xalt_id, [meeting(title="Neu")])
        await _run(storage, backend)

        puts = [r for r in backend.requests if r.method == "PUT"]
        assert puts[-1].headers["If-Match"] == current_etag

    async def test_time_shift_replaces_the_copy(self, env) -> None:
        # A moved appointment gets a new source key (the start is part of the
        # identity), so the old copy has to go and a new one appear.
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        old_url = next(iter(backend.own))

        store_events(
            storage,
            xalt_id,
            [meeting(start=datetime(2026, 7, 20, 12, 0, tzinfo=UTC))],
        )
        result = await _run(storage, backend)

        assert result.inserted == 1
        assert result.deleted == 1
        assert old_url not in backend.own
        assert len(backend.own) == 1
        assert storage.count_mirror_events() == 1
        (_, ics), = list(backend.own.values())
        assert "DTSTART:20260720T120000Z" in ics

    async def test_manually_deleted_copy_is_recreated(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        backend.own.clear()  # somebody removed the copy in Nextcloud

        result = await _run(storage, backend)
        assert result.inserted == 1
        assert len(backend.own) == 1


@pytest.mark.anyio
class TestCopiedDetails:
    """Details reach the copy and stay in sync (Etappe 45, part B).

    Includes the one-off migration of the copies that already live in
    Roland's calendar: their mapping rows predate the columns, so they read
    as "details unknown" and must be rewritten exactly once.
    """

    def descriptions(self, backend: RecordingCaldav) -> str:
        (_, ics), = list(backend.own.values())
        return ics.replace("\r\n ", "")

    async def test_details_are_written_into_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(
            storage,
            xalt_id,
            [
                meeting(
                    description="Agenda",
                    organizer="Chef <c@x>",
                    attendees="Chef <c@x>\nRoland <r@x>",
                )
            ],
        )
        backend = RecordingCaldav()
        await _run(storage, backend)

        ics = self.descriptions(backend)
        assert "DESCRIPTION:Eingeladen von: Chef <c@x>" in ics
        assert "Teilnehmer:\\n- Chef <c@x>\\n- Roland <r@x>" in ics
        assert "Agenda" in ics

    async def test_changed_description_updates_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting(description="Alte Agenda")])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(description="Neue Agenda")])
        result = await _run(storage, backend)

        assert result.updated == 1
        assert "Neue Agenda" in self.descriptions(backend)

    async def test_changed_attendees_update_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting(attendees="A <a@x>")])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(attendees="A <a@x>\nB <b@x>")])
        result = await _run(storage, backend)

        assert result.updated == 1
        assert "- B <b@x>" in self.descriptions(backend)

    async def test_changed_organizer_updates_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting(organizer="A <a@x>")])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [meeting(organizer="B <b@x>")])
        result = await _run(storage, backend)

        assert result.updated == 1
        assert "Eingeladen von: B <b@x>" in self.descriptions(backend)

    async def test_unchanged_details_are_a_null_diff(self, env) -> None:
        storage, xalt_id, _ = env
        event = meeting(description="Agenda", organizer="Chef <c@x>")
        store_events(storage, xalt_id, [event])
        backend = RecordingCaldav()
        await _run(storage, backend)
        puts_after_insert = len([r for r in backend.requests if r.method == "PUT"])

        result = await _run(storage, backend)

        assert (result.inserted, result.updated, result.deleted) == (0, 0, 0)
        assert len([r for r in backend.requests if r.method == "PUT"]) == (
            puts_after_insert
        )

    async def test_an_event_without_details_is_never_rewritten(self, env) -> None:
        """Most private appointments carry nothing — they must stay untouched."""
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        result = await _run(storage, backend)
        assert (result.inserted, result.updated) == (0, 0)

    async def test_existing_copies_are_migrated_exactly_once(self, env) -> None:
        """The 192 live copies: one rewrite, then a null diff forever.

        Simulated the way it will really happen — a mapping row and a copy
        written before the columns existed (hence no details anywhere), while
        the source event now carries them.
        """
        storage, xalt_id, _ = env
        detailed = meeting(description="Agenda", organizer="Chef <c@x>")
        plain = meeting()  # same key, no details: what was written back then
        key = source_key(xalt_id, detailed)
        backend = RecordingCaldav()
        url = backend.seed_own(key, plain)
        storage.upsert_mirror_event(
            MirrorEvent(
                source_key=key,
                resource_url=url,
                etag=backend.own[url][0],
                start=plain.start,
                end=plain.end,
                all_day=False,
                title=plain.title,
                location=None,
            ),
            updated_at=NOW,
        )
        store_events(storage, xalt_id, [detailed])

        first = await _run(storage, backend)
        assert (first.updated, first.inserted, first.deleted) == (1, 0, 0)
        assert "Agenda" in self.descriptions(backend)

        second = await _run(storage, backend)
        assert (second.updated, second.inserted, second.deleted) == (0, 0, 0)


@pytest.mark.anyio
class TestDelete:
    async def test_vanished_appointment_deletes_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [])
        result = await _run(storage, backend, source_results={xalt_id: None})

        assert result.deleted == 1
        assert backend.own == {}
        assert storage.count_mirror_events() == 0

    async def test_deselecting_one_source_deletes_only_its_copies(self, env) -> None:
        storage, xalt_id, _ = env
        other = storage.add_source(type="google", name="Zweitkalender", config={})
        settings.set_mirror_sync_source_ids(storage, [xalt_id, other])
        store_events(storage, xalt_id, [meeting(uid="a")])
        store_events(storage, other, [meeting(uid="b", title="Zweiter Termin")])
        backend = RecordingCaldav()
        await _run(storage, backend)
        assert len(backend.own) == 2

        settings.set_mirror_sync_source_ids(storage, [xalt_id])
        result = await _run(storage, backend, source_results={xalt_id: None})

        assert result.deleted == 1
        assert backend.summaries() == {"Kundentermin"}

    async def test_delete_is_conditional(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        etag = next(iter(backend.own.values()))[0]

        store_events(storage, xalt_id, [])
        await _run(storage, backend, source_results={xalt_id: None})

        delete = [r for r in backend.requests if r.method == "DELETE"][-1]
        assert delete.headers["If-Match"] == etag

    async def test_already_gone_copy_is_not_an_error(self, env) -> None:
        storage, xalt_id, _ = env
        event = meeting()
        key = source_key(xalt_id, event)
        storage.upsert_mirror_event(
            MirrorEvent(
                source_key=key,
                resource_url=resource_url(COLLECTION, key),
                etag='"stale"',
                start=event.start,
                end=event.end,
                all_day=False,
                title=event.title,
            ),
            updated_at=NOW,
        )
        backend = RecordingCaldav()  # the resource does not exist any more
        result = await _run(storage, backend, source_results={xalt_id: None})
        assert result.error is None
        assert result.deleted == 1
        assert storage.count_mirror_events() == 0


@pytest.mark.anyio
class TestDataLossGuard:
    """No attacker needed: an unlucky run must not empty the real calendar.

    Same lesson as the slideshow index guard — the criterion for deleting is
    "the sources were read without error in THIS run", never "the result set
    happens to be empty".
    """

    async def _seeded(self, env) -> RecordingCaldav:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        assert len(backend.own) == 1
        return backend

    async def test_failed_source_skips_the_run_and_keeps_the_copies(
        self, env
    ) -> None:
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        # The source failed this run; storage.sync_events therefore never
        # replaced the window and the events may be stale — but a failing
        # source could equally have wiped the window in an earlier run.
        store_events(storage, xalt_id, [])
        before = len(backend.requests)

        result = await _run(
            storage, backend, source_results={xalt_id: "Zeitüberschreitung"}
        )

        assert result.skipped is True
        assert result.skip_reason == "source_error"
        assert result.deleted == 0
        assert len(backend.own) == 1  # the copy survived
        assert storage.count_mirror_events() == 1
        assert len(backend.requests) == before  # not even a REPORT was sent

    async def test_empty_result_without_source_information_skips(self, env) -> None:
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        store_events(storage, xalt_id, [])

        result = await _run(storage, backend)  # no per-source information

        assert result.skipped is True
        assert result.skip_reason == "empty_result"
        assert len(backend.own) == 1
        assert storage.count_mirror_events() == 1

    async def test_empty_result_after_a_clean_sync_cleans_up(self, env) -> None:
        # The legitimate case: Roland really deleted every appointment and the
        # source reported success — the copies must go.
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        store_events(storage, xalt_id, [])

        result = await _run(storage, backend, source_results={xalt_id: None})

        assert result.skipped is False
        assert result.deleted == 1
        assert backend.own == {}

    async def test_orphan_copies_without_mapping_also_hold_the_run_back(
        self, env
    ) -> None:
        # Mapping lost (restored DB) but copies still in Nextcloud: an empty
        # desired set would delete all of them as orphans.
        storage, _, _ = env
        backend = RecordingCaldav()
        url = backend.seed_own("9|weg|2026-07-20T08:15:00+00:00", meeting())

        result = await _run(storage, backend)

        assert result.skipped is True
        assert result.skip_reason == "empty_result"
        assert url in backend.own

    async def test_nothing_to_lose_is_not_a_skip(self, env) -> None:
        # First run on a fresh install: no mapping, no copies, no events.
        storage, _, _ = env
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.skipped is False
        assert result.error is None

    async def test_empty_source_selection_skips_instead_of_deleting(
        self, env
    ) -> None:
        # A missing or unparseable mirror_sync_source_ids setting reads as
        # the empty list, indistinguishable from "deliberately deselected".
        storage, _, _ = env
        backend = await self._seeded(env)
        settings.set_mirror_sync_source_ids(storage, [])
        before = len(backend.requests)

        result = await _run(storage, backend, source_results={})

        assert result.skipped is True
        assert result.skip_reason == "no_sources"
        assert len(backend.own) == 1
        assert storage.count_mirror_events() == 1
        assert len(backend.requests) == before

    async def test_empty_selection_without_mapping_is_not_a_skip(self, env) -> None:
        storage, _, _ = env
        settings.set_mirror_sync_source_ids(storage, [])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.skipped is False

    async def test_skip_is_visible_in_the_status(self, env) -> None:
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        store_events(storage, xalt_id, [])
        await _run(storage, backend, source_results={xalt_id: "kaputt"})

        status = settings.get_mirror_sync_status(storage)
        assert status["skipped"] is True
        assert status["skip_reason"] == "source_error"
        assert status["error"] is None  # a skipped run is not a failed run
        assert status["active_mirrors"] == 1

    async def test_a_normal_run_clears_the_skip_flag(self, env) -> None:
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        store_events(storage, xalt_id, [])
        await _run(storage, backend, source_results={xalt_id: "kaputt"})
        assert settings.get_mirror_sync_status(storage)["skipped"] is True

        store_events(storage, xalt_id, [meeting()])
        await _run(storage, backend, source_results={xalt_id: None})
        status = settings.get_mirror_sync_status(storage)
        assert status["skipped"] is False
        assert status["skip_reason"] is None

    async def test_a_skipped_run_logs_nothing(self, env) -> None:
        storage, xalt_id, _ = env
        backend = await self._seeded(env)
        before = len(storage.get_audit_entries("2026-01-01T00:00:00+00:00"))
        store_events(storage, xalt_id, [])
        await _run(storage, backend, source_results={xalt_id: "kaputt"})
        after = len(storage.get_audit_entries("2026-01-01T00:00:00+00:00"))
        assert before == after


@pytest.mark.anyio
class TestStaleMapping:
    """A mapping row pointing outside the CURRENT collection is discarded.

    Roland can repoint the target source's ``calendar_url`` with a plain
    PATCH on the source — that path does not clear the mapping, so its URLs
    suddenly lie outside the collection. Aborting the run there would wedge
    the mirror permanently at the same spot on every following run.
    """

    def _stale_row(self, storage: Storage, url: str) -> str:
        event = meeting(uid="alt")
        key = source_key(99, event)
        storage.upsert_mirror_event(
            MirrorEvent(
                source_key=key,
                resource_url=url,
                etag='"stale"',
                start=event.start,
                end=event.end,
                all_day=False,
                title=event.title,
            ),
            updated_at=NOW,
        )
        return key

    async def test_url_outside_the_collection_drops_the_row_and_run_continues(
        self, env
    ) -> None:
        storage, xalt_id, _ = env
        key = self._stale_row(
            storage,
            "https://cloud.example.com/remote.php/dav/calendars/roland/alt/x.ics",
        )
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()

        result = await _run(storage, backend)

        assert result.error is None
        assert result.inserted == 1  # the run finished its normal work
        assert result.deleted == 0  # nothing was deleted on the server
        assert key not in {row.source_key for row in storage.list_mirror_events()}
        assert all(str(r.url).startswith(COLLECTION) for r in backend.requests)

    async def test_dropping_a_stale_row_is_not_logged_as_a_deletion(self, env) -> None:
        storage, xalt_id, _ = env
        self._stale_row(storage, "https://evil.example.com/x.ics")
        backend = RecordingCaldav()
        await _run(storage, backend, source_results={xalt_id: None})
        entries = storage.get_audit_entries("2026-01-01T00:00:00+00:00")
        assert entries == []


@pytest.mark.anyio
class TestWindowBoundaries:
    async def test_past_appointment_is_not_mirrored(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(
            storage, xalt_id, [meeting(start=datetime(2026, 7, 8, 8, tzinfo=UTC))]
        )
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 0

    async def test_appointment_beyond_180_days_is_not_mirrored(self, env) -> None:
        storage, xalt_id, _ = env
        far = NOW + timedelta(days=MIRROR_SYNC_FUTURE_DAYS + 5)
        store_events(storage, xalt_id, [meeting(start=far)])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.inserted == 0


@pytest.mark.anyio
class TestInvariantAndOrphans:
    async def test_foreign_events_are_never_touched(self, env) -> None:
        # The backend raises if any request targets a foreign resource. The
        # listing reports them, but they carry no marker, so they are dropped
        # while parsing and never become write or delete candidates.
        storage, xalt_id, _ = env
        foreign_url = COLLECTION + "chef-jour-fixe.ics"
        backend = RecordingCaldav(
            foreign={
                foreign_url: (
                    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//X//EN\r\n"
                    "BEGIN:VEVENT\r\nUID:chef@example\r\n"
                    "DTSTAMP:20260701T000000Z\r\n"
                    "DTSTART:20260720T080000Z\r\nDTEND:20260720T090000Z\r\n"
                    "SUMMARY:Wichtiger fremder Termin\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n"
                )
            }
        )
        store_events(storage, xalt_id, [meeting()])
        await _run(storage, backend)  # insert phase
        store_events(storage, xalt_id, [meeting(title="Anders")])
        await _run(storage, backend)  # update phase
        store_events(storage, xalt_id, [])
        # delete + orphan phase (the source reported success, so the empty
        # result is trustworthy and the cleanup runs)
        await _run(storage, backend, source_results={xalt_id: None})
        assert foreign_url in backend.foreign  # still there, never requested

    async def test_foreign_event_carrying_the_marker_is_never_deleted(self, env) -> None:
        # The marker property name is public (open source), so an invitation
        # can arrive carrying it — that alone must not turn a real appointment
        # into a deletion candidate. The backend raises if it is ever targeted.
        storage, xalt_id, _ = env
        foreign_url = COLLECTION + "wichtiger-kundentermin.ics"
        backend = RecordingCaldav(
            foreign={
                foreign_url: (
                    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Angreifer//EN\r\n"
                    "BEGIN:VEVENT\r\nUID:echter-kundentermin@example.com\r\n"
                    "DTSTAMP:20260701T000000Z\r\n"
                    "DTSTART:20260721T090000Z\r\nDTEND:20260721T100000Z\r\n"
                    "SUMMARY:Wichtiger Kundentermin\r\n"
                    "X-FAMILIENKALENDER-OWNER:1\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n"
                )
            }
        )
        store_events(storage, xalt_id, [meeting()])
        result = await _run(storage, backend)
        assert result.orphans_removed == 0
        assert foreign_url in backend.foreign  # still there, never requested

    async def test_orphan_copy_without_mapping_is_removed(self, env) -> None:
        storage, xalt_id, _ = env
        backend = RecordingCaldav()
        url = backend.seed_own("9|weg|2026-07-20T08:15:00+00:00", meeting())
        result = await _run(storage, backend, source_results={xalt_id: None})
        assert result.orphans_removed == 1
        assert url not in backend.own

    async def test_duplicate_copy_of_a_wanted_appointment_is_removed(self, env) -> None:
        storage, xalt_id, _ = env
        event = meeting()
        store_events(storage, xalt_id, [event])
        backend = RecordingCaldav()
        await _run(storage, backend)
        # A second resource for the same source key (interrupted run, restore).
        key = source_key(xalt_id, event)
        duplicate = backend.seed_own(key, event, url=COLLECTION + "kopie-2.ics")

        result = await _run(storage, backend)
        assert result.orphans_removed == 1
        assert duplicate not in backend.own
        # The mapping row of the surviving copy is untouched.
        assert storage.count_mirror_events() == 1

    async def test_every_request_stays_inside_the_collection(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        assert all(str(r.url).startswith(COLLECTION) for r in backend.requests)


@pytest.mark.anyio
class TestConflicts:
    async def test_conflict_on_update_leaves_the_copy_alone(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        url = next(iter(backend.own))
        backend.conflict_urls.add(url)

        store_events(storage, xalt_id, [meeting(title="Neu")])
        result = await _run(storage, backend)

        assert result.conflicts == 1
        assert result.updated == 0
        assert result.error is None  # a conflict is not a failed run
        assert backend.summaries() == {"Kundentermin"}

    async def test_conflict_on_delete_keeps_the_mapping_for_a_retry(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        url = next(iter(backend.own))
        backend.conflict_urls.add(url)

        store_events(storage, xalt_id, [])
        result = await _run(storage, backend, source_results={xalt_id: None})
        assert result.conflicts == 1
        assert storage.count_mirror_events() == 1

        # Next run without the conflict: the deletion goes through.
        backend.conflict_urls.clear()
        result2 = await _run(storage, backend, source_results={xalt_id: None})
        assert result2.deleted == 1
        assert backend.own == {}


@pytest.mark.anyio
class TestErrorIsolation:
    async def test_server_error_is_recorded_not_raised(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        ) as http:
            result = await run_mirror_sync(storage, now=NOW, client=http)

        assert result.error is not None
        assert settings.get_mirror_sync_status(storage)["error"] is not None

    async def test_successful_run_clears_the_error_and_reports_status(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        status = settings.get_mirror_sync_status(storage)
        assert status["error"] is None
        assert status["active_mirrors"] == 1
        assert status["last_run"] is not None


@pytest.mark.anyio
class TestOutgoingChangeLog:
    async def test_insert_update_delete_are_logged(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        store_events(storage, xalt_id, [meeting(title="Kundentermin neu")])
        await _run(storage, backend)
        store_events(storage, xalt_id, [])
        await _run(storage, backend, source_results={xalt_id: None})

        entries = [
            e
            for e in storage.get_audit_entries("2026-01-01T00:00:00+00:00")
            if e.direction == "out"
        ]
        assert {e.action for e in entries} == {"added", "updated", "removed"}
        assert all(e.scope == "MoreValue (Spiegel)" for e in entries)
        removed = next(e for e in entries if e.action == "removed")
        # The source appointment is gone by then — the title comes from the
        # mapping so the deletion stays traceable.
        assert removed.title == "Kundentermin neu"

    async def test_nulldiff_run_logs_nothing(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        before = len(storage.get_audit_entries("2026-01-01T00:00:00+00:00"))
        await _run(storage, backend)
        after = len(storage.get_audit_entries("2026-01-01T00:00:00+00:00"))
        assert before == after == 1


@pytest.mark.anyio
class TestNoFeedbackLoop:
    async def test_own_copy_read_back_produces_no_event_and_no_busy_block(
        self, env
    ) -> None:
        """The copies never re-enter the system through the CalDAV reader.

        This is the loop guard: were the copy read back, it would land in the
        events table (duplicate in views/feed) and the busy sync would mirror
        it into Xalt as a "Busy MV" block.
        """
        from app.sources.caldav import fetch_events

        storage, xalt_id, target_id = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        assert len(backend.own) == 1

        multistatus = backend._report().content

        def caldav_read(request: httpx.Request) -> httpx.Response:
            return httpx.Response(207, content=multistatus)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(caldav_read)
        ) as http:
            read_back = await fetch_events(
                TARGET_CONFIG,
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 8, 1, tzinfo=UTC),
                client=http,
            )

        assert read_back == []
        # And nothing lands in the target source's events either.
        store_events(storage, target_id, read_back)
        assert storage.get_events(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
        ) != []  # the Xalt original is still there
        assert all(
            item.source_id != target_id
            for item in storage.get_events(
                datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
            )
        )


@pytest.mark.anyio
class TestPendingCleanup:
    """Copies left behind in a calendar that is no longer the target.

    Roland created a new Nextcloud calendar and wants the mirrored copies
    MOVED there. Merely repointing the target used to leave the old 155
    copies lying around and create 155 more — duplicating, not moving. The
    admin therefore QUEUES a cleanup for the old calendar; the next mirror run
    drains it before doing anything else.
    """

    async def test_queued_cleanup_removes_the_copies_of_the_old_calendar(
        self, env
    ) -> None:
        storage, xalt_id, target_id = env
        old = RecordingCaldav()
        old.seed_own(source_key(xalt_id, meeting("a")), meeting("a"))
        old.seed_own(source_key(xalt_id, meeting("b")), meeting("b"))
        # The admin's side of a target switch / clean stop.
        settings.queue_mirror_sync_cleanup(storage, target_id)
        storage.clear_mirror_events()
        settings.set_mirror_sync_enabled(storage, False)

        result = await _run(storage, old)

        assert result.cleaned_up == 2
        assert old.own == {}
        assert settings.get_mirror_sync_cleanup(storage)["pending"] == []

    async def test_switching_the_target_moves_the_copies(self, env) -> None:
        storage, xalt_id, target_id = env
        old = RecordingCaldav()
        new_target_id = add_other_target_source(storage)
        new = RecordingCaldav(collection=OTHER_COLLECTION)
        store_events(storage, xalt_id, [meeting()])
        await _run(storage, old, source_results={xalt_id: None})
        assert len(old.own) == 1

        # What the admin PUT does when the target changes.
        settings.queue_mirror_sync_cleanup(storage, target_id)
        storage.clear_mirror_events()
        settings.set_mirror_sync_target_source_id(storage, new_target_id)

        cluster = CaldavCluster(old, new)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(cluster.handler)
        ) as http:
            result = await run_mirror_sync(
                storage, now=NOW, client=http, source_results={xalt_id: None}
            )

        assert result.cleaned_up == 1
        assert old.own == {}
        assert new.summaries() == {"Kundentermin"}

    async def test_cleanup_never_touches_foreign_resources(self, env) -> None:
        storage, xalt_id, target_id = env
        foreign_url = f"{COLLECTION}echter-termin.ics"
        old = RecordingCaldav(
            foreign={
                foreign_url: build_ical("x", meeting("real"))
                .decode()
                .replace("X-FAMILIENKALENDER-OWNER:1\r\n", "")
                .replace("X-FAMILIENKALENDER-MIRROR:x\r\n", "")
            }
        )
        old.seed_own(source_key(xalt_id, meeting("a")), meeting("a"))
        settings.queue_mirror_sync_cleanup(storage, target_id)
        storage.clear_mirror_events()
        settings.set_mirror_sync_enabled(storage, False)

        result = await _run(storage, old)  # the mock raises on a foreign target

        assert result.cleaned_up == 1
        assert old.own == {}
        assert foreign_url in old.foreign

    async def test_vanished_old_target_never_blocks_the_run(self, env) -> None:
        """Source deleted / credentials gone: warn, drop, carry on."""
        storage, xalt_id, _ = env
        settings.queue_mirror_sync_cleanup(storage, 9999)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()

        result = await _run(storage, backend, source_results={xalt_id: None})

        # The normal mirror run happened regardless.
        assert result.inserted == 1
        assert result.error is None
        state = settings.get_mirror_sync_cleanup(storage)
        assert state["pending"] == []
        assert state["failed"] is True

    async def test_unreachable_old_target_is_retried_then_given_up(self, env) -> None:
        storage, _xalt_id, target_id = env
        settings.queue_mirror_sync_cleanup(storage, target_id)
        storage.clear_mirror_events()
        settings.set_mirror_sync_enabled(storage, False)
        backend = RecordingCaldav()
        backend.report_status = 503

        for attempt in range(1, MAX_CLEANUP_ATTEMPTS):
            result = await _run(storage, backend)
            assert result.error is None
            state = settings.get_mirror_sync_cleanup(storage)
            assert state["pending"] == [
                {"source_id": target_id, "attempts": attempt}
            ]
            assert state["failed"] is False

        result = await _run(storage, backend)
        state = settings.get_mirror_sync_cleanup(storage)
        assert state["pending"] == []
        assert state["failed"] is True

    async def test_cleanup_for_the_active_target_is_dropped(self, env) -> None:
        """Target switched away and back before the drain ran.

        The mirror is actively maintaining that calendar again, so the stale
        order must not delete what the sync just wrote.
        """
        storage, xalt_id, target_id = env
        settings.queue_mirror_sync_cleanup(storage, target_id)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()

        result = await _run(storage, backend, source_results={xalt_id: None})

        assert result.cleaned_up == 0
        assert result.inserted == 1
        assert settings.get_mirror_sync_cleanup(storage)["pending"] == []

    async def test_cleanup_removals_are_written_to_the_change_log(self, env) -> None:
        storage, xalt_id, target_id = env
        old = RecordingCaldav()
        old.seed_own(source_key(xalt_id, meeting("a")), meeting("a"))
        settings.queue_mirror_sync_cleanup(storage, target_id)
        storage.clear_mirror_events()
        settings.set_mirror_sync_enabled(storage, False)

        await _run(storage, old)

        entries = storage.get_audit_entries("2026-01-01T00:00:00+00:00")
        assert [(e.direction, e.scope, e.action) for e in entries] == [
            ("out", "MoreValue (Spiegel)", "removed")
        ]
        assert entries[0].title == "Kundentermin"

    async def test_without_a_queue_a_disabled_mirror_still_does_nothing(
        self, env
    ) -> None:
        storage, xalt_id, _ = env
        settings.set_mirror_sync_enabled(storage, False)
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        result = await _run(storage, backend)
        assert result.cleaned_up == 0
        assert backend.requests == []


class TestResourceNaming:
    def test_copies_never_use_the_foreign_uid(self) -> None:
        event = meeting(uid="foreign-uid@xalt.example")
        name = resource_name(source_key(3, event))
        assert name.startswith("familienkalender-mirror-")
        assert "foreign-uid" not in name


@pytest.mark.anyio
class TestDeclinedAppointmentDisappears:
    """Declining a Google invitation must remove the MoreValue copy.

    Roland's report: an appointment he declines stays (struck through) in
    Google, "aber im MV Kalender ist der Termin weiterhin unverändert, hier
    müsste er entfernt werden". The read client drops declined items, so the
    ordinary mirror diff does the removal — this test wires both halves
    together instead of assuming they meet.
    """

    async def _google_events(self, tmp_path: Path, item: dict) -> list[CalendarEvent]:
        token_file = tmp_path / "google.json"
        save_tokens(
            token_file,
            {
                "client_id": "c",
                "client_secret": "s",
                "refresh_token": "r",
                "access_token": "a",
                "access_token_expires_at": (
                    datetime.now(UTC) + timedelta(hours=5)
                ).isoformat(),
            },
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [item]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await google.fetch_events(
                {"calendar_id": "roland@xalt.example"},
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2028, 1, 1, tzinfo=UTC),
                token_file=token_file,
                client=http,
            )

    async def test_copy_is_deleted_after_the_invitation_is_declined(
        self, env, tmp_path: Path
    ) -> None:
        storage, xalt_id, _ = env
        invitation = {
            "id": "u-invite",
            "status": "confirmed",
            "summary": "Kundentermin",
            "start": {"dateTime": "2026-07-20T08:15:00+00:00"},
            "end": {"dateTime": "2026-07-20T09:15:00+00:00"},
            "attendees": [
                {"email": "r@xalt.example", "self": True, "responseStatus": "accepted"}
            ],
        }
        accepted = await self._google_events(tmp_path, invitation)
        store_events(storage, xalt_id, accepted)
        backend = RecordingCaldav()
        first = await _run(storage, backend, source_results={xalt_id: None})
        assert first.inserted == 1
        assert backend.summaries() == {"Kundentermin"}

        declined_item = {
            **invitation,
            "attendees": [
                {"email": "r@xalt.example", "self": True, "responseStatus": "declined"}
            ],
        }
        declined = await self._google_events(tmp_path, declined_item)
        assert declined == []  # the read client already dropped it
        store_events(storage, xalt_id, declined)

        second = await _run(storage, backend, source_results={xalt_id: None})

        assert second.deleted == 1
        assert backend.own == {}
        assert storage.count_mirror_events() == 0
