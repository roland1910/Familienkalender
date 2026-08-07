"""Tests for the birthday sync (contact birthdays -> Xalt and/or MoreValue).

Focus, in this order:

1. the marker separation — three syncs now write into the same two calendars,
   and none of them may ever touch another's entries (tested from BOTH
   directions, with mock servers that raise on a forbidden request),
2. the yearly series: one recurring entry per person instead of one copy per
   year, with a stable person key across year boundaries and 29 February
   handled deliberately,
3. the diff (new / renamed / gone), orphan cleanup and the data-loss guards.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import settings
from app.birthday_sync import (
    CALDAV_AUDIT_SCOPE,
    GOOGLE_AUDIT_SCOPE,
    TARGET_CALDAV,
    TARGET_GOOGLE,
    birthday_sync_window,
    occurs_in_window,
    person_key,
    run_birthday_sync,
    series_start,
)
from app.busy_sync import run_busy_sync
from app.caldav_write import (
    BIRTHDAY_NAMESPACE,
    build_ical,
    resource_url,
)
from app.google_busy import (
    BIRTHDAY_MARKER_KEY,
    MARKER_KEY,
    OWNER_KEY,
    OWNER_VALUE,
    OWNER_VALUE_BIRTHDAY,
    busy_write_token_path,
)
from app.mirror_sync import run_mirror_sync
from app.models import BirthdayBlock, CalendarEvent
from app.sources.google import save_tokens
from app.storage import Storage

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
COLLECTION = "https://cloud.example.com/remote.php/dav/calendars/roland/mv/"
CALDAV_CONFIG = {
    "url": "https://cloud.example.com",
    "username": "roland",
    "app_password": "secret",
    "calendar_url": COLLECTION,
}


def make_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def write_write_token(path: Path) -> None:
    save_tokens(
        path,
        {
            "client_id": "cid",
            "client_secret": "cs",
            "refresh_token": "rt",
            "access_token": "at",
            "access_token_expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )


def add_contact_source(storage: Storage, name: str = "Geburtstage Roland") -> int:
    return storage.add_source(type="google_contacts", name=name, config={})


def add_caldav_source(storage: Storage, name: str = "Roland MV") -> int:
    return storage.add_source(type="caldav", name=name, config=dict(CALDAV_CONFIG))


def add_birthday_event(
    storage: Storage,
    source_id: int,
    resource: str,
    when: date,
    name: str = "Oma",
) -> None:
    """Store one occurrence exactly as app.sources.google_contacts emits it."""
    event = CalendarEvent(
        uid=f"{resource}|{when.year}",
        title=f"🎂 {name}",
        start=when,
        end=when + timedelta(days=1),
        all_day=True,
    )
    storage.sync_events(
        source_id,
        [event],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
        synced_at=NOW,
    )


class FakeBackend:
    """One transport serving both Google and the CalDAV collection.

    Foreign objects live next to the add-on's own ones, and any request that
    targets a foreign object raises — that is what turns the "only our own
    entries" invariant into an assertion instead of a comment.
    """

    def __init__(self) -> None:
        # Google: id -> event dict (the add-on's own, any purpose)
        self.google: dict[str, dict] = {}
        self.google_foreign: set[str] = set()
        # CalDAV: url -> (etag, ics text) for our own objects
        self.caldav: dict[str, tuple[str, str]] = {}
        self.caldav_foreign: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.conflict_urls: set[str] = set()
        self._counter = 0

    # -- seeding ------------------------------------------------------
    def seed_google_birthday(self, key: str, title: str, start: date) -> str:
        self._counter += 1
        event_id = f"bday-{self._counter}"
        self.google[event_id] = {
            "id": event_id,
            "summary": title,
            "start": {"date": start.isoformat()},
            "end": {"date": (start + timedelta(days=1)).isoformat()},
            "recurrence": ["RRULE:FREQ=YEARLY"],
            "extendedProperties": {
                "private": {
                    BIRTHDAY_MARKER_KEY: key,
                    OWNER_KEY: OWNER_VALUE_BIRTHDAY,
                }
            },
        }
        return event_id

    def seed_google_busy_block(self, source_key: str) -> str:
        self._counter += 1
        event_id = f"busy-{self._counter}"
        self.google[event_id] = {
            "id": event_id,
            "summary": "Busy MV",
            "start": {"dateTime": "2026-07-20T08:00:00+00:00"},
            "end": {"dateTime": "2026-07-20T09:00:00+00:00"},
            "extendedProperties": {
                "private": {MARKER_KEY: source_key, OWNER_KEY: OWNER_VALUE}
            },
        }
        return event_id

    def seed_caldav(self, ics: str, url: str) -> str:
        self._counter += 1
        etag = f'"e{self._counter}"'
        self.caldav[url] = (etag, ics)
        return url

    # -- transport ----------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        if url in self.caldav_foreign:
            raise AssertionError(f"foreign CalDAV resource {url} was targeted!")
        if url.startswith(COLLECTION) or url == COLLECTION.rstrip("/"):
            return self._caldav(request, url)
        return self._google(request)

    def _google(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for fid in self.google_foreign:
            if path.endswith(f"/events/{fid}"):
                raise AssertionError(f"foreign Google event {fid} was targeted!")
        if request.method == "GET" and path.endswith("/events"):
            query = request.url.params["privateExtendedProperty"]
            key, _, value = query.partition("=")
            items = [
                item
                for item in self.google.values()
                if (item.get("extendedProperties") or {})
                .get("private", {})
                .get(key)
                == value
            ]
            return httpx.Response(200, json={"items": items})
        if request.method == "POST" and path.endswith("/events"):
            self._counter += 1
            event_id = f"new-{self._counter}"
            body = json.loads(request.content)
            body["id"] = event_id
            self.google[event_id] = body
            return httpx.Response(200, json={"id": event_id})
        event_id = path.rsplit("/", 1)[-1]
        if request.method == "PATCH":
            if event_id not in self.google:
                return httpx.Response(404)
            body = json.loads(request.content)
            body["id"] = event_id
            self.google[event_id] = body
            return httpx.Response(200, json={"id": event_id})
        if request.method == "DELETE":
            self.google.pop(event_id, None)
            return httpx.Response(204)
        return httpx.Response(405)

    def _caldav(self, request: httpx.Request, url: str) -> httpx.Response:
        if request.method == "REPORT":
            entries = [(u, etag, ics) for u, (etag, ics) in self.caldav.items()]
            entries += [(u, '"f"', ics) for u, ics in self.caldav_foreign.items()]
            responses = "".join(
                f"<d:response><d:href>{escape(u)}</d:href>"
                f"<d:propstat><d:prop><d:getetag>{escape(etag)}</d:getetag>"
                f"<cal:calendar-data>{escape(ics)}</cal:calendar-data></d:prop>"
                f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                for u, etag, ics in entries
            )
            body = (
                '<?xml version="1.0"?>'
                '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
                f"{responses}</d:multistatus>"
            )
            return httpx.Response(207, content=body.encode())
        if url in self.conflict_urls:
            return httpx.Response(412)
        if request.method == "PUT":
            if request.headers.get("If-None-Match") == "*" and url in self.caldav:
                return httpx.Response(412)
            self._counter += 1
            etag = f'"e{self._counter}"'
            self.caldav[url] = (etag, request.content.decode())
            return httpx.Response(204, headers={"ETag": etag})
        if request.method == "DELETE":
            if url not in self.caldav:
                return httpx.Response(404)
            del self.caldav[url]
            return httpx.Response(204)
        return httpx.Response(405)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # -- assertions helpers -------------------------------------------
    def google_birthdays(self) -> list[dict]:
        return [
            item
            for item in self.google.values()
            if (item.get("extendedProperties") or {})
            .get("private", {})
            .get(OWNER_KEY)
            == OWNER_VALUE_BIRTHDAY
        ]

    def caldav_summaries(self) -> set[str]:
        return {
            line.split(":", 1)[1].strip()
            for _, ics in self.caldav.values()
            for line in ics.splitlines()
            if line.startswith("SUMMARY:")
        }


def enable(
    storage: Storage,
    *,
    source_ids: list[int],
    google: bool = True,
    caldav_id: int | None = None,
) -> None:
    settings.set_birthday_sync_source_ids(storage, source_ids)
    settings.set_birthday_sync_google_enabled(storage, google)
    settings.set_birthday_sync_caldav_target_id(storage, caldav_id)
    settings.set_birthday_sync_enabled(storage, True)


@pytest.fixture
def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = busy_write_token_path()
    write_write_token(path)
    return path


class TestPersonIdentity:
    def test_person_key_ignores_the_year_of_the_source_uid(self) -> None:
        assert person_key(6, "people/c1|2026") == person_key(6, "people/c1|2027")
        assert person_key(6, "people/c1|2026") == "6|people/c1"

    def test_person_key_separates_the_two_contact_sources(self) -> None:
        assert person_key(6, "people/c1|2026") != person_key(7, "people/c1|2026")

    def test_a_resource_without_a_year_suffix_survives_untouched(self) -> None:
        assert person_key(6, "people/c1") == "6|people/c1"

    def test_only_a_four_digit_year_suffix_is_stripped(self) -> None:
        assert person_key(6, "people/c1|abc") == "6|people/c1|abc"


class TestSeriesStart:
    def test_leap_day_birthday_starts_on_28_february(self) -> None:
        """A yearly RRULE from 29 February would only fire every four years."""
        assert series_start(date(2028, 2, 29)) == date(2028, 2, 28)

    def test_every_other_date_is_kept(self) -> None:
        assert series_start(date(2026, 8, 20)) == date(2026, 8, 20)
        assert series_start(date(2027, 2, 28)) == date(2027, 2, 28)


class TestOccursInWindow:
    def test_date_inside_the_window(self) -> None:
        assert occurs_in_window(date(2020, 8, 20), date(2026, 7, 2), date(2026, 10, 7))

    def test_date_outside_the_window(self) -> None:
        assert not occurs_in_window(
            date(2020, 12, 24), date(2026, 7, 2), date(2026, 10, 7)
        )

    def test_window_spanning_the_turn_of_the_year(self) -> None:
        assert occurs_in_window(date(1990, 1, 3), date(2026, 12, 20), date(2027, 2, 1))


@pytest.mark.anyio
class TestWritingTheSeries:
    async def test_creates_one_yearly_series_in_both_targets(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], caldav_id=caldav_id)
        backend = FakeBackend()

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.inserted == 2
        google = backend.google_birthdays()
        assert len(google) == 1
        assert google[0]["summary"] == "🎂 Oma"
        assert google[0]["recurrence"] == ["RRULE:FREQ=YEARLY"]
        assert google[0]["start"] == {"date": "2026-08-20"}
        ics = next(iter(backend.caldav.values()))[1]
        assert "RRULE:FREQ=YEARLY" in ics
        assert "DTSTART;VALUE=DATE:20260820" in ics
        # Both targets are mapped separately.
        assert storage.count_birthday_blocks(TARGET_GOOGLE) == 1
        assert storage.count_birthday_blocks(TARGET_CALDAV) == 1

    async def test_only_the_selected_target_is_written(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], google=False, caldav_id=caldav_id)
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert backend.google_birthdays() == []
        assert storage.count_birthday_blocks(TARGET_CALDAV) == 1
        assert storage.count_birthday_blocks(TARGET_GOOGLE) == 0

    async def test_a_second_run_changes_nothing(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], caldav_id=caldav_id)
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert (result.inserted, result.updated, result.deleted) == (0, 0, 0)
        assert result.orphans_removed == 0

    async def test_next_years_occurrence_does_not_rewrite_the_series(
        self, tmp_path: Path, token: Path
    ) -> None:
        """The year of the series start is irrelevant for a yearly rule.

        Every year the source hands out a new occurrence; comparing its year
        would rewrite every series once a year for nothing.
        """
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )
            # A year later the source only knows the 2027 occurrence.
            add_birthday_event(storage, source_id, "people/c1", date(2027, 8, 20))
            later = NOW + timedelta(days=365)
            result = await run_birthday_sync(
                storage, now=later, client=http, source_results={source_id: None}
            )

        assert result.updated == 0
        assert len(backend.google_birthdays()) == 1

    async def test_renaming_a_contact_updates_the_series(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], caldav_id=caldav_id)
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )
            add_birthday_event(
                storage, source_id, "people/c1", date(2026, 8, 20), name="Oma Müller"
            )
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.updated == 2
        assert backend.google_birthdays()[0]["summary"] == "🎂 Oma Müller"
        assert backend.caldav_summaries() == {"🎂 Oma Müller"}

    async def test_a_moved_birthday_updates_the_series_start(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )
            add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 21))
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert backend.google_birthdays()[0]["start"] == {"date": "2026-08-21"}

    async def test_a_leap_day_birthday_is_written_on_28_february(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        # The source clamps 29 Feb to the 28th in non-leap years already; in a
        # leap year it emits the 29th, and the series still starts on the 28th
        # so the yearly rule fires EVERY year.
        add_birthday_event(storage, source_id, "people/c9", date(2028, 2, 29))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        leap_now = datetime(2028, 2, 1, 12, 0, tzinfo=UTC)

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=leap_now, client=http, source_results={source_id: None}
            )

        assert backend.google_birthdays()[0]["start"] == {"date": "2028-02-28"}


@pytest.mark.anyio
class TestDeletionOnlyOnKnowledge:
    async def test_a_removed_contact_in_the_window_is_deleted(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )
            # The contact loses its birthday: the source emits nothing at all.
            storage.sync_events(
                source_id,
                [],
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
                synced_at=NOW,
            )
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.deleted == 1
        assert backend.google_birthdays() == []
        assert storage.list_birthday_blocks() == []

    async def test_a_person_outside_the_window_is_never_deleted(
        self, tmp_path: Path, token: Path
    ) -> None:
        """Absence outside the window says nothing — most people are absent.

        The events table only holds -7/+90 days, so a December birthday has no
        source event in July. Deleting on that basis would wipe almost every
        series on every run.
        """
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        winter_id = backend.seed_google_birthday(
            "6|people/winter", "🎂 Opa", date(2025, 12, 24)
        )
        storage.upsert_birthday_block(
            BirthdayBlock(
                person_key="6|people/winter",
                target=TARGET_GOOGLE,
                remote_id=winter_id,
                start=date(2025, 12, 24),
                title="🎂 Opa",
            ),
            updated_at=NOW,
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.deleted == 0
        assert result.orphans_removed == 0
        assert winter_id in backend.google

    async def test_an_unmapped_series_outside_the_window_is_adopted(
        self, tmp_path: Path, token: Path
    ) -> None:
        """A lost mapping must not turn every out-of-window series into a orphan."""
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        winter_id = backend.seed_google_birthday(
            "6|people/winter", "🎂 Opa", date(2025, 12, 24)
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.orphans_removed == 0
        assert winter_id in backend.google
        adopted = [
            row for row in storage.list_birthday_blocks() if row.remote_id == winter_id
        ]
        assert adopted and adopted[0].person_key == "6|people/winter"

    async def test_an_unmapped_series_inside_the_window_is_removed(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        stale_id = backend.seed_google_birthday(
            "6|people/gone", "🎂 Weg", date(2025, 8, 1)
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.orphans_removed == 1
        assert stale_id not in backend.google


@pytest.mark.anyio
class TestMarkerSeparation:
    """No sync may ever delete another sync's entries as its own orphan."""

    async def test_the_birthday_sync_never_touches_a_busy_block(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        busy_id = backend.seed_google_busy_block("3|uid-1|2026-07-20T08:00:00+00:00")
        backend.google_foreign.add(busy_id)

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert busy_id in backend.google

    async def test_the_busy_sync_never_touches_a_birthday_series(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        mv_id = add_caldav_source(storage, name="Roland MV")
        settings.set_busy_sync_source_ids(storage, [mv_id])
        settings.set_busy_sync_enabled(storage, True)
        backend = FakeBackend()
        bday_id = backend.seed_google_birthday(
            "6|people/c1", "🎂 Oma", date(2026, 8, 20)
        )
        # Any request against it is a violation of the invariant.
        backend.google_foreign.add(bday_id)

        async with backend.client() as http:
            await run_busy_sync(storage, now=NOW, client=http)

        assert bday_id in backend.google

    async def test_the_birthday_sync_never_touches_a_mirror_copy(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], caldav_id=caldav_id)
        backend = FakeBackend()
        mirror_key = "3|xalt-uid|2026-07-20T10:15:00+00:00"
        mirror_event = CalendarEvent(
            uid="xalt-uid",
            title="Kundentermin",
            start=datetime(2026, 7, 20, 10, 15, tzinfo=UTC),
            end=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
            all_day=False,
        )
        mirror_url = resource_url(COLLECTION, mirror_key)
        backend.seed_caldav(build_ical(mirror_key, mirror_event).decode(), mirror_url)

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert mirror_url in backend.caldav
        assert "Kundentermin" in backend.caldav_summaries()

    async def test_the_mirror_sync_never_touches_a_birthday_series(
        self, tmp_path: Path
    ) -> None:
        storage = make_storage(tmp_path)
        xalt_id = storage.add_source(
            type="google", name="Roland@Xalt", config={"calendar_id": "primary"}
        )
        caldav_id = add_caldav_source(storage)
        settings.set_mirror_sync_source_ids(storage, [xalt_id])
        settings.set_mirror_sync_target_source_id(storage, caldav_id)
        settings.set_mirror_sync_enabled(storage, True)
        storage.sync_events(
            xalt_id,
            [
                CalendarEvent(
                    uid="xalt-uid",
                    title="Kundentermin",
                    start=datetime(2026, 7, 20, 10, 15, tzinfo=UTC),
                    end=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
                    all_day=False,
                )
            ],
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            synced_at=NOW,
        )
        backend = FakeBackend()
        series = CalendarEvent(
            uid="6|people/c1",
            title="🎂 Oma",
            start=date(2026, 8, 20),
            end=date(2026, 8, 21),
            all_day=True,
        )
        bday_url = resource_url(COLLECTION, "6|people/c1", namespace=BIRTHDAY_NAMESPACE)
        backend.seed_caldav(
            build_ical(
                "6|people/c1", series, namespace=BIRTHDAY_NAMESPACE, yearly=True
            ).decode(),
            bday_url,
        )
        # The mirror must never address it — not even to delete it as orphan.
        backend.caldav_foreign[bday_url] = backend.caldav.pop(bday_url)[1]

        async with backend.client() as http:
            await run_mirror_sync(
                storage, now=NOW, client=http, source_results={xalt_id: None}
            )

        assert bday_url in backend.caldav_foreign


@pytest.mark.anyio
class TestDataLossGuard:
    async def test_a_failed_source_skips_the_run(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        existing = backend.seed_google_birthday(
            "6|people/c1", "🎂 Oma", date(2026, 8, 20)
        )
        storage.upsert_birthday_block(
            BirthdayBlock(
                person_key="6|people/c1",
                target=TARGET_GOOGLE,
                remote_id=existing,
                start=date(2026, 8, 20),
                title="🎂 Oma",
            ),
            updated_at=NOW,
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage,
                now=NOW,
                client=http,
                source_results={source_id: "HTTP 502"},
            )

        assert result.skipped is True
        assert result.skip_reason == "source_error"
        assert existing in backend.google
        status = settings.get_birthday_sync_status(storage)
        assert status["skipped"] is True
        assert status["error"] is None

    async def test_no_selected_source_skips_while_series_exist(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        add_contact_source(storage)
        enable(storage, source_ids=[])
        backend = FakeBackend()
        existing = backend.seed_google_birthday(
            "6|people/c1", "🎂 Oma", date(2026, 8, 20)
        )
        storage.upsert_birthday_block(
            BirthdayBlock(
                person_key="6|people/c1",
                target=TARGET_GOOGLE,
                remote_id=existing,
                start=date(2026, 8, 20),
                title="🎂 Oma",
            ),
            updated_at=NOW,
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={}
            )

        assert result.skip_reason == "no_sources"
        assert existing in backend.google

    async def test_empty_result_without_verified_sources_skips(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        existing = backend.seed_google_birthday(
            "6|people/c1", "🎂 Oma", date(2026, 8, 20)
        )

        async with backend.client() as http:
            # source_results=None: a standalone call knows nothing about the
            # freshness of the sources.
            result = await run_birthday_sync(storage, now=NOW, client=http)

        assert result.skip_reason == "empty_result"
        assert existing in backend.google

    async def test_a_verified_empty_source_may_clean_up(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        enable(storage, source_ids=[source_id])
        backend = FakeBackend()
        stale = backend.seed_google_birthday("6|people/c1", "🎂 Oma", date(2026, 8, 20))
        storage.upsert_birthday_block(
            BirthdayBlock(
                person_key="6|people/c1",
                target=TARGET_GOOGLE,
                remote_id=stale,
                start=date(2026, 8, 20),
                title="🎂 Oma",
            ),
            updated_at=NOW,
        )

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.skipped is False
        assert result.deleted == 1
        assert stale not in backend.google


@pytest.mark.anyio
class TestSwitchesAndErrors:
    async def test_disabled_sync_writes_nothing(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        settings.set_birthday_sync_source_ids(storage, [source_id])
        settings.set_birthday_sync_google_enabled(storage, True)
        backend = FakeBackend()

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.inserted == 0
        assert backend.requests == []

    async def test_no_usable_target_is_a_no_op(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], google=False, caldav_id=None)
        backend = FakeBackend()

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.inserted == 0
        assert backend.requests == []

    async def test_a_non_contact_source_is_ignored(
        self, tmp_path: Path, token: Path
    ) -> None:
        """Turning every appointment of a normal calendar into a YEARLY series
        would be a mess — only contact sources feed this sync."""
        storage = make_storage(tmp_path)
        caldav_id = add_caldav_source(storage)
        storage.sync_events(
            caldav_id,
            [
                CalendarEvent(
                    uid="mv-1",
                    title="Betriebsausflug",
                    start=date(2026, 8, 20),
                    end=date(2026, 8, 21),
                    all_day=True,
                )
            ],
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
            synced_at=NOW,
        )
        enable(storage, source_ids=[caldav_id])
        backend = FakeBackend()

        async with backend.client() as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={caldav_id: None}
            )

        # No selected (contact) source left -> the guard refuses, nothing runs.
        assert result.inserted == 0
        assert backend.google_birthdays() == []

    async def test_a_server_error_is_isolated_and_recorded(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id])

        def failing(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://oauth2.googleapis.com/token":
                return httpx.Response(200, json={"access_token": "at", "expires_in": 60})
            return httpx.Response(500)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(failing)
        ) as http:
            result = await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        assert result.error is not None
        assert settings.get_birthday_sync_status(storage)["error"] is not None


@pytest.mark.anyio
class TestChangeLog:
    async def test_writes_are_logged_with_their_own_scopes(
        self, tmp_path: Path, token: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = add_contact_source(storage)
        caldav_id = add_caldav_source(storage)
        add_birthday_event(storage, source_id, "people/c1", date(2026, 8, 20))
        enable(storage, source_ids=[source_id], caldav_id=caldav_id)
        backend = FakeBackend()

        async with backend.client() as http:
            await run_birthday_sync(
                storage, now=NOW, client=http, source_results={source_id: None}
            )

        entries = storage.get_audit_entries("2020-01-01T00:00:00+00:00")
        scopes = {entry.scope for entry in entries}
        assert scopes == {GOOGLE_AUDIT_SCOPE, CALDAV_AUDIT_SCOPE}
        assert all(entry.direction == "out" for entry in entries)
        assert all(entry.action == "added" for entry in entries)
        assert all(entry.title == "🎂 Oma" for entry in entries)
        assert all(entry.event_start == "2026-08-20" for entry in entries)


class TestWindow:
    def test_window_matches_the_stored_event_range(self) -> None:
        start, end = birthday_sync_window(NOW)
        assert start.date() == date(2026, 7, 2)
        assert end.date() == date(2026, 10, 7)
