"""Tests for the CalDAV client (Nextcloud) with mocked HTTP transport."""

from datetime import UTC, date, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.sources import limits
from app.sources.caldav import fetch_events, list_calendars

FIXTURES = Path(__file__).parent / "fixtures" / "caldav"
BERLIN = ZoneInfo("Europe/Berlin")

CONFIG = {
    "url": "https://cloud.example.com",
    "username": "roland",
    "app_password": "secret-app-password",
    "calendar_url": "https://cloud.example.com/remote.php/dav/calendars/roland/firma/",
}

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 31, tzinfo=UTC)


def multistatus_report(*ics_texts: str) -> str:
    responses = "".join(
        f"<d:response>"
        f"<d:href>/remote.php/dav/calendars/roland/firma/{index}.ics</d:href>"
        f"<d:propstat><d:prop>"
        f"<cal:calendar-data>{escape(text)}</cal:calendar-data>"
        f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        f"</d:response>"
        for index, text in enumerate(ics_texts)
    )
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">'
        f"{responses}</d:multistatus>"
    )


def make_client(
    xml_body: str, captured: list[httpx.Request], status_code: int = 207
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, content=xml_body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.anyio
class TestFetchEvents:
    async def test_sends_calendar_query_report_with_time_range(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(multistatus_report(), captured) as client:
            await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        request = captured[0]
        assert request.method == "REPORT"
        assert str(request.url) == CONFIG["calendar_url"]
        assert request.headers["Depth"] == "1"
        assert "authorization" in request.headers
        body = request.content.decode()
        assert "calendar-query" in body
        assert 'start="20260701T000000Z"' in body
        assert 'end="20260731T000000Z"' in body

    async def test_parses_simple_event(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("simple.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        assert len(events) == 1
        event = events[0]
        assert event.uid == "simple-1@example.com"
        assert event.title == "Elternabend"
        assert event.location == "Schule"
        assert event.all_day is False
        assert event.start == datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN)
        assert event.end == datetime(2026, 7, 10, 19, 30, tzinfo=BERLIN)

    async def test_expands_recurring_event_within_window(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("recurring.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        starts = sorted(event.start for event in events)
        # Weekly on Mondays from 2026-07-06; the 2026-07-20 instance is moved
        # to 20:00 by a RECURRENCE-ID override.
        assert starts == [
            datetime(2026, 7, 6, 18, 0, tzinfo=BERLIN),
            datetime(2026, 7, 13, 18, 0, tzinfo=BERLIN),
            datetime(2026, 7, 20, 20, 0, tzinfo=BERLIN),
            datetime(2026, 7, 27, 18, 0, tzinfo=BERLIN),
        ]
        moved = next(e for e in events if e.start == datetime(2026, 7, 20, 20, 0, tzinfo=BERLIN))
        assert moved.title == "Sporttraining (verschoben)"
        assert all(event.uid == "weekly-1@example.com" for event in events)

    async def test_recurring_event_is_not_expanded_beyond_window(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("recurring.ics"))
        narrow_end = datetime(2026, 7, 8, tzinfo=UTC)
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, narrow_end, client=client)

        assert [event.start for event in events] == [datetime(2026, 7, 6, 18, 0, tzinfo=BERLIN)]

    async def test_parses_all_day_event_as_dates(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("allday.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        assert len(events) == 1
        event = events[0]
        assert event.all_day is True
        assert event.start == date(2026, 7, 12)
        assert event.end == date(2026, 7, 15)

    async def test_floating_event_without_dtend_gets_local_tz(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("floating.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        assert len(events) == 1
        event = events[0]
        assert event.all_day is False
        assert event.start.tzinfo is not None
        assert event.start.astimezone(BERLIN).hour == 19

    async def test_aggregates_events_from_multiple_resources(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("simple.ics"), fixture("allday.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        assert {event.uid for event in events} == {
            "simple-1@example.com",
            "allday-1@example.com",
        }

    async def test_http_error_raises(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client("kaputt", captured, status_code=500) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)


@pytest.mark.anyio
class TestFetchLimits:
    async def test_declared_content_length_over_limit_aborts(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                207,
                headers={"Content-Length": str(limits.MAX_RESPONSE_BYTES + 1)},
                content=b"",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(limits.SyncLimitExceededError, match="10"):
                await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

    async def test_streamed_body_over_limit_aborts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.sources.limits.MAX_RESPONSE_BYTES", 1024)

        class ChunkStream(httpx.AsyncByteStream):
            """Chunked body without a Content-Length header."""

            async def __aiter__(self):
                for _ in range(8):
                    yield b"X" * 512

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(207, stream=ChunkStream())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(limits.SyncLimitExceededError):
                await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

    async def test_occurrence_cap_aborts_with_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # recurring.ics expands to 4 occurrences inside the window.
        monkeypatch.setattr("app.sources.limits.MAX_EVENTS_PER_SOURCE", 3)
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("recurring.ics"))
        async with make_client(xml, captured) as client:
            with pytest.raises(limits.SyncLimitExceededError, match="3"):
                await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

    async def test_events_below_caps_are_unaffected(self) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(fixture("simple.ics"))
        async with make_client(xml, captured) as client:
            events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)
        assert len(events) == 1


BROKEN_ICS = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=KAPUTT:???\nEND:VCALENDAR"


@pytest.mark.anyio
class TestPerEventErrorIsolation:
    async def test_broken_ical_blob_does_not_abort_the_fetch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        captured: list[httpx.Request] = []
        xml = multistatus_report(BROKEN_ICS, fixture("simple.ics"), fixture("allday.ics"))
        async with make_client(xml, captured) as client:
            with caplog.at_level("WARNING", logger="app.sources.caldav"):
                events = await fetch_events(CONFIG, WINDOW_START, WINDOW_END, client=client)

        # The two valid events survive the broken foreign invitation.
        assert {event.uid for event in events} == {
            "simple-1@example.com",
            "allday-1@example.com",
        }
        # The failure is counted and logged — without leaking raw event data.
        assert any("1" in record.getMessage() for record in caplog.records)
        assert all("KAPUTT" not in record.getMessage() for record in caplog.records)


PROPFIND_MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav">
  <d:response>
    <d:href>/remote.php/dav/calendars/roland/</d:href>
    <d:propstat>
      <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/roland/firma/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Firma</d:displayname>
        <d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>
        <cal:supported-calendar-component-set>
          <cal:comp name="VEVENT"/><cal:comp name="VTODO"/>
        </cal:supported-calendar-component-set>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/calendars/roland/aufgaben/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Aufgaben</d:displayname>
        <d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>
        <cal:supported-calendar-component-set>
          <cal:comp name="VTODO"/>
        </cal:supported-calendar-component-set>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


@pytest.mark.anyio
class TestListCalendars:
    async def test_lists_only_event_calendars(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(PROPFIND_MULTISTATUS, captured) as client:
            calendars = await list_calendars(CONFIG, client=client)

        assert calendars == [
            {
                "name": "Firma",
                "url": "https://cloud.example.com/remote.php/dav/calendars/roland/firma/",
            }
        ]

    async def test_sends_propfind_to_calendar_home(self) -> None:
        captured: list[httpx.Request] = []
        async with make_client(PROPFIND_MULTISTATUS, captured) as client:
            await list_calendars(CONFIG, client=client)

        request = captured[0]
        assert request.method == "PROPFIND"
        assert str(request.url) == "https://cloud.example.com/remote.php/dav/calendars/roland/"
        assert request.headers["Depth"] == "1"
