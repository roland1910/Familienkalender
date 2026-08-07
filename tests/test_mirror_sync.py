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
    MIRROR_SYNC_FUTURE_DAYS,
    mirror_sync_window,
    run_mirror_sync,
)
from app.models import CalendarEvent, MirrorEvent
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


def make_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


class RecordingCaldav:
    """Mock Nextcloud modelling one calendar collection.

    Holds foreign (unmarked) resources next to the add-on's own copies and
    raises the moment any request targets a foreign resource — that is what
    turns the invariant into an assertion instead of a comment.
    """

    def __init__(self, foreign: dict[str, str] | None = None) -> None:
        # url -> (etag, ics text) for the add-on's own copies
        self.own: dict[str, tuple[str, str]] = {}
        # url -> ics text for real Nextcloud appointments
        self.foreign: dict[str, str] = dict(foreign or {})
        self.requests: list[httpx.Request] = []
        self.conflict_urls: set[str] = set()
        self._etag = 0

    def seed_own(self, key: str, event: CalendarEvent, *, url: str | None = None) -> str:
        target = url or resource_url(COLLECTION, key)
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


def add_target_source(storage: Storage) -> int:
    return storage.add_source(type="caldav", name="Roland MV", config=TARGET_CONFIG)


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
) -> CalendarEvent:
    begin = start or datetime(2026, 7, 20, 8, 15, tzinfo=UTC)
    return CalendarEvent(
        uid=uid,
        title=title,
        start=begin,
        end=begin + timedelta(hours=hours),
        all_day=False,
        location=location,
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


async def _run(storage: Storage, backend: RecordingCaldav):
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend.handler)) as http:
        return await run_mirror_sync(storage, now=NOW, client=http)


class TestWindow:
    def test_window_is_180_days_from_today(self) -> None:
        start, end = mirror_sync_window(NOW)
        assert start == datetime(2026, 7, 9, tzinfo=BERLIN)
        assert (end - start).days == MIRROR_SYNC_FUTURE_DAYS


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
class TestDelete:
    async def test_vanished_appointment_deletes_the_copy(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        store_events(storage, xalt_id, [])
        result = await _run(storage, backend)

        assert result.deleted == 1
        assert backend.own == {}
        assert storage.count_mirror_events() == 0

    async def test_deselecting_the_source_deletes_its_copies(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)

        settings.set_mirror_sync_source_ids(storage, [])
        result = await _run(storage, backend)

        assert result.deleted == 1
        assert backend.own == {}

    async def test_delete_is_conditional(self, env) -> None:
        storage, xalt_id, _ = env
        store_events(storage, xalt_id, [meeting()])
        backend = RecordingCaldav()
        await _run(storage, backend)
        etag = next(iter(backend.own.values()))[0]

        store_events(storage, xalt_id, [])
        await _run(storage, backend)

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
        result = await _run(storage, backend)
        assert result.error is None
        assert result.deleted == 1
        assert storage.count_mirror_events() == 0


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
        await _run(storage, backend)  # delete + orphan phase
        assert foreign_url in backend.foreign  # still there, never requested

    async def test_orphan_copy_without_mapping_is_removed(self, env) -> None:
        storage, _, _ = env
        backend = RecordingCaldav()
        url = backend.seed_own("9|weg|2026-07-20T08:15:00+00:00", meeting())
        result = await _run(storage, backend)
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
        result = await _run(storage, backend)
        assert result.conflicts == 1
        assert storage.count_mirror_events() == 1

        # Next run without the conflict: the deletion goes through.
        backend.conflict_urls.clear()
        result2 = await _run(storage, backend)
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
        await _run(storage, backend)

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


class TestResourceNaming:
    def test_copies_never_use_the_foreign_uid(self) -> None:
        event = meeting(uid="foreign-uid@xalt.example")
        name = resource_name(source_key(3, event))
        assert name.startswith("familienkalender-mirror-")
        assert "foreign-uid" not in name
