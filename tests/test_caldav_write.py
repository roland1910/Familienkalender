"""Tests for the CalDAV write client (Nextcloud) with mocked HTTP transport.

Covers the iCalendar generation (times, all-day semantics, marker
properties, what is deliberately NOT copied), the conditional requests
(If-Match / If-None-Match, 412 handling) and — most importantly — the
security invariant: the client only ever addresses resources inside the
configured collection, and only reports components carrying its own marker
as its own.
"""

from datetime import UTC, date, datetime
from xml.sax.saxutils import escape

import httpx
import pytest

from app.caldav_write import (
    MIRROR_UID_PREFIX,
    CaldavConflictError,
    CaldavWriteClient,
    CaldavWriteError,
    build_ical,
    mirror_uid,
    resource_name,
    resource_url,
)
from app.models import MIRROR_MARKER_PROP, MIRROR_OWNER_PROP, CalendarEvent
from app.url_validation import SourceURLError

COLLECTION = "https://cloud.example.com/remote.php/dav/calendars/roland/mv/"
CONFIG = {
    "url": "https://cloud.example.com",
    "username": "roland",
    "app_password": "secret-app-password",
    "calendar_url": COLLECTION,
}

WINDOW_START = datetime(2026, 7, 9, tzinfo=UTC)
WINDOW_END = datetime(2027, 1, 5, tzinfo=UTC)

TIMED_EVENT = CalendarEvent(
    uid="foreign-uid@xalt.example",
    title="Kundentermin",
    start=datetime(2026, 7, 20, 10, 15, tzinfo=UTC),
    end=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
    all_day=False,
    location="Besprechungsraum 2",
)

ALL_DAY_EVENT = CalendarEvent(
    uid="foreign-day@xalt.example",
    title="Betriebsausflug",
    start=date(2026, 7, 20),
    end=date(2026, 7, 21),
    all_day=True,
)

SOURCE_KEY = "3|foreign-uid@xalt.example|2026-07-20T10:15:00+00:00"


def make_client(
    handler, captured: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def multistatus(*entries: tuple[str, str, str]) -> bytes:
    """Build a REPORT multistatus from (href, etag, ics-text) triples."""
    responses = "".join(
        f"<d:response><d:href>{escape(href)}</d:href>"
        f"<d:propstat><d:prop>"
        f"<d:getetag>{escape(etag)}</d:getetag>"
        f"<cal:calendar-data>{escape(ics)}</cal:calendar-data>"
        f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        f"</d:response>"
        for href, etag, ics in entries
    )
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        f"{responses}</d:multistatus>"
    ).encode()


def foreign_ics(uid: str = "boss-1:1@example.com") -> str:
    """A real Nextcloud appointment — no marker, must never count as ours."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Foreign//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        "DTSTAMP:20260701T000000Z\r\n"
        "DTSTART:20260720T080000Z\r\nDTEND:20260720T090000Z\r\n"
        "SUMMARY:Fremder Termin\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class TestIcalGeneration:
    def test_timed_event_carries_title_times_and_location(self) -> None:
        text = build_ical(
            SOURCE_KEY, TIMED_EVENT, now=datetime(2026, 7, 9, 12, tzinfo=UTC)
        ).decode()
        assert "SUMMARY:Kundentermin" in text
        assert "DTSTART:20260720T101500Z" in text
        assert "DTEND:20260720T110000Z" in text
        assert "LOCATION:Besprechungsraum 2" in text

    def test_description_and_attendees_are_not_copied(self) -> None:
        text = build_ical(SOURCE_KEY, TIMED_EVENT).decode()
        assert "DESCRIPTION" not in text
        assert "ATTENDEE" not in text
        assert "ORGANIZER" not in text

    def test_all_day_event_uses_exclusive_date_range(self) -> None:
        text = build_ical("1|d|2026-07-20", ALL_DAY_EVENT).decode()
        assert "DTSTART;VALUE=DATE:20260720" in text
        assert "DTEND;VALUE=DATE:20260721" in text

    def test_marker_properties_identify_the_copy(self) -> None:
        text = build_ical(SOURCE_KEY, TIMED_EVENT).decode()
        assert f"{MIRROR_OWNER_PROP}:1" in text
        # The source key is escaped per RFC 5545 but must round-trip.
        assert MIRROR_MARKER_PROP in text
        assert "foreign-uid@xalt.example" in text

    def test_uid_is_our_own_never_the_foreign_one(self) -> None:
        text = build_ical(SOURCE_KEY, TIMED_EVENT).decode()
        assert f"UID:{mirror_uid(SOURCE_KEY)}" in text
        assert mirror_uid(SOURCE_KEY).startswith(MIRROR_UID_PREFIX)
        assert "UID:foreign-uid@xalt.example" not in text

    def test_uid_is_deterministic_and_key_specific(self) -> None:
        assert mirror_uid(SOURCE_KEY) == mirror_uid(SOURCE_KEY)
        assert mirror_uid(SOURCE_KEY) != mirror_uid(SOURCE_KEY + "x")

    def test_resource_url_stays_inside_the_collection(self) -> None:
        url = resource_url(COLLECTION, SOURCE_KEY)
        assert url == COLLECTION + resource_name(SOURCE_KEY)
        assert url.endswith(".ics")

    def test_collection_without_trailing_slash_is_normalized(self) -> None:
        url = resource_url(COLLECTION.rstrip("/"), SOURCE_KEY)
        assert url == COLLECTION + resource_name(SOURCE_KEY)


@pytest.mark.anyio
class TestWriteRequests:
    async def test_create_sends_put_with_if_none_match(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, headers={"ETag": '"etag-1"'})

        url = resource_url(COLLECTION, SOURCE_KEY)
        async with make_client(handler, captured) as http:
            etag = await CaldavWriteClient(CONFIG, http).create_event(
                url, SOURCE_KEY, TIMED_EVENT
            )

        assert etag == '"etag-1"'
        request = captured[0]
        assert request.method == "PUT"
        assert str(request.url) == url
        assert request.headers["If-None-Match"] == "*"
        assert "authorization" in request.headers
        assert request.headers["Content-Type"].startswith("text/calendar")

    async def test_update_sends_put_with_if_match(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204, headers={"ETag": '"etag-2"'})

        url = resource_url(COLLECTION, SOURCE_KEY)
        async with make_client(handler, captured) as http:
            etag = await CaldavWriteClient(CONFIG, http).update_event(
                url, SOURCE_KEY, TIMED_EVENT, etag='"etag-1"'
            )

        assert etag == '"etag-2"'
        assert captured[0].headers["If-Match"] == '"etag-1"'

    async def test_update_without_known_etag_still_requires_existence(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(lambda r: httpx.Response(204), captured) as http:
            await CaldavWriteClient(CONFIG, http).update_event(
                resource_url(COLLECTION, SOURCE_KEY), SOURCE_KEY, TIMED_EVENT, etag=""
            )
        assert captured[0].headers["If-Match"] == "*"

    async def test_delete_sends_if_match(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(lambda r: httpx.Response(204), captured) as http:
            await CaldavWriteClient(CONFIG, http).delete_event(
                resource_url(COLLECTION, SOURCE_KEY), etag='"etag-1"'
            )
        assert captured[0].method == "DELETE"
        assert captured[0].headers["If-Match"] == '"etag-1"'

    async def test_delete_of_missing_resource_is_success(self) -> None:
        async with make_client(lambda r: httpx.Response(404)) as http:
            await CaldavWriteClient(CONFIG, http).delete_event(
                resource_url(COLLECTION, SOURCE_KEY)
            )

    async def test_put_conflict_raises_conflict_error(self) -> None:
        async with make_client(lambda r: httpx.Response(412)) as http:
            with pytest.raises(CaldavConflictError):
                await CaldavWriteClient(CONFIG, http).update_event(
                    resource_url(COLLECTION, SOURCE_KEY),
                    SOURCE_KEY,
                    TIMED_EVENT,
                    etag='"stale"',
                )

    async def test_delete_conflict_raises_conflict_error(self) -> None:
        async with make_client(lambda r: httpx.Response(412)) as http:
            with pytest.raises(CaldavConflictError):
                await CaldavWriteClient(CONFIG, http).delete_event(
                    resource_url(COLLECTION, SOURCE_KEY), etag='"stale"'
                )

    async def test_server_error_raises_write_error(self) -> None:
        async with make_client(lambda r: httpx.Response(500)) as http:
            with pytest.raises(CaldavWriteError):
                await CaldavWriteClient(CONFIG, http).create_event(
                    resource_url(COLLECTION, SOURCE_KEY), SOURCE_KEY, TIMED_EVENT
                )


@pytest.mark.anyio
class TestUrlInvariant:
    async def test_write_outside_the_collection_is_refused(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(lambda r: httpx.Response(204), captured) as http:
            client = CaldavWriteClient(CONFIG, http)
            with pytest.raises(CaldavWriteError):
                await client.create_event(
                    "https://cloud.example.com/remote.php/dav/calendars/roland/privat/x.ics",
                    SOURCE_KEY,
                    TIMED_EVENT,
                )
        assert captured == []  # nothing was ever sent

    async def test_delete_outside_the_collection_is_refused(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(lambda r: httpx.Response(204), captured) as http:
            client = CaldavWriteClient(CONFIG, http)
            with pytest.raises(CaldavWriteError):
                await client.delete_event("https://evil.example.com/x.ics")
        assert captured == []

    async def test_non_https_collection_is_refused(self) -> None:
        async with make_client(lambda r: httpx.Response(204)) as http:
            with pytest.raises(SourceURLError):
                CaldavWriteClient({**CONFIG, "calendar_url": "ftp://x/y/"}, http)


@pytest.mark.anyio
class TestListOwnResources:
    async def test_only_marked_resources_are_returned(self) -> None:
        own = build_ical(SOURCE_KEY, TIMED_EVENT).decode()
        body = multistatus(
            ("/remote.php/dav/calendars/roland/mv/foreign.ics", '"e0"', foreign_ics()),
            (
                f"/remote.php/dav/calendars/roland/mv/{resource_name(SOURCE_KEY)}",
                '"e1"',
                own,
            ),
        )
        captured: list[httpx.Request] = []
        async with make_client(
            lambda r: httpx.Response(207, content=body), captured
        ) as http:
            found = await CaldavWriteClient(CONFIG, http).list_own_resources(
                WINDOW_START, WINDOW_END
            )

        assert len(found) == 1
        assert found[0].source_key == SOURCE_KEY
        assert found[0].etag == '"e1"'
        assert found[0].url == resource_url(COLLECTION, SOURCE_KEY)
        assert found[0].summary == "Kundentermin"
        assert captured[0].method == "REPORT"
        assert 'start="20260709T000000Z"' in captured[0].content.decode()

    async def test_href_outside_the_collection_is_dropped(self) -> None:
        own = build_ical(SOURCE_KEY, TIMED_EVENT).decode()
        body = multistatus(("https://evil.example.com/pwned.ics", '"e1"', own))
        async with make_client(lambda r: httpx.Response(207, content=body)) as http:
            found = await CaldavWriteClient(CONFIG, http).list_own_resources(
                WINDOW_START, WINDOW_END
            )
        assert found == []

    async def test_unparseable_object_is_skipped(self) -> None:
        body = multistatus(
            ("/remote.php/dav/calendars/roland/mv/broken.ics", '"e1"', "NOT ICAL")
        )
        async with make_client(lambda r: httpx.Response(207, content=body)) as http:
            found = await CaldavWriteClient(CONFIG, http).list_own_resources(
                WINDOW_START, WINDOW_END
            )
        assert found == []

    async def test_report_error_raises(self) -> None:
        async with make_client(lambda r: httpx.Response(500)) as http:
            with pytest.raises(CaldavWriteError):
                await CaldavWriteClient(CONFIG, http).list_own_resources(
                    WINDOW_START, WINDOW_END
                )
