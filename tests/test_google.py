"""Tests for the Google Calendar client (REST via httpx, mocked transport)."""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.sources.google import fetch_events, load_tokens, save_tokens, token_path

BERLIN = ZoneInfo("Europe/Berlin")

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 29, tzinfo=UTC)

CONFIG = {"calendar_id": "marina@example.com"}

TOKEN_URL = "https://oauth2.googleapis.com/token"

TIMED_ITEM = {
    "id": "evt-timed",
    "status": "confirmed",
    "summary": "Kinderarzt",
    "location": "Praxis Dr. Muster",
    "start": {"dateTime": "2026-07-10T15:30:00+02:00", "timeZone": "Europe/Berlin"},
    "end": {"dateTime": "2026-07-10T16:00:00+02:00", "timeZone": "Europe/Berlin"},
}

ALL_DAY_ITEM = {
    "id": "evt-allday",
    "status": "confirmed",
    "summary": "Sommerferien",
    "start": {"date": "2026-07-30"},
    "end": {"date": "2026-08-03"},
}

CANCELLED_ITEM = {
    "id": "evt-cancelled",
    "status": "cancelled",
    "start": {"dateTime": "2026-07-11T10:00:00+02:00"},
    "end": {"dateTime": "2026-07-11T11:00:00+02:00"},
}

UNTITLED_ITEM = {
    "id": "evt-untitled",
    "status": "confirmed",
    "start": {"dateTime": "2026-07-12T18:00:00+02:00"},
    "end": {"dateTime": "2026-07-12T19:00:00+02:00"},
}


def write_tokens(path: Path, *, expired: bool = False) -> dict:
    expires_at = datetime.now(UTC) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    tokens = {
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token-1",
        "access_token": "access-token-old",
        "access_token_expires_at": expires_at.isoformat(),
    }
    save_tokens(path, tokens)
    return tokens


def make_client(
    captured: list[httpx.Request],
    *,
    pages: list[dict],
    reject_tokens: set[str] | None = None,
) -> httpx.AsyncClient:
    """Mock transport serving the token endpoint and paginated events.list."""
    page_iter = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "access-token-new", "expires_in": 3599}
            )
        token = request.headers.get("Authorization", "")
        if reject_tokens and token.removeprefix("Bearer ") in reject_tokens:
            return httpx.Response(401, json={"error": {"code": 401}})
        return httpx.Response(200, json=next(page_iter))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
class TestFetchEvents:
    async def test_maps_timed_event(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": [TIMED_ITEM]}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert len(events) == 1
        event = events[0]
        assert event.uid == "evt-timed"
        assert event.title == "Kinderarzt"
        assert event.location == "Praxis Dr. Muster"
        assert event.all_day is False
        assert event.start == datetime(2026, 7, 10, 15, 30, tzinfo=BERLIN)
        assert event.end == datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)

    async def test_maps_all_day_event_to_dates(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": [ALL_DAY_ITEM]}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        event = events[0]
        assert event.all_day is True
        assert event.start == date(2026, 7, 30)
        assert event.end == date(2026, 8, 3)

    async def test_skips_cancelled_events(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [{"items": [CANCELLED_ITEM, TIMED_ITEM]}]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert [event.uid for event in events] == ["evt-timed"]

    async def test_missing_summary_becomes_empty_title(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": [UNTITLED_ITEM]}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert events[0].title == ""

    async def test_requests_single_events_within_window(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": []}]) as client:
            await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        request = captured[0]
        assert "marina%40example.com/events" in str(request.url)
        params = dict(request.url.params)
        assert params["singleEvents"] == "true"
        assert params["timeMin"] == "2026-07-01T00:00:00+00:00"
        assert params["timeMax"] == "2026-09-29T00:00:00+00:00"

    async def test_follows_pagination(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [
            {"items": [TIMED_ITEM], "nextPageToken": "page-2"},
            {"items": [ALL_DAY_ITEM]},
        ]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert {event.uid for event in events} == {"evt-timed", "evt-allday"}
        assert dict(captured[1].url.params)["pageToken"] == "page-2"


@pytest.mark.anyio
class TestTokenHandling:
    async def test_valid_token_is_used_without_refresh(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": []}]) as client:
            await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert all(str(request.url) != TOKEN_URL for request in captured)
        assert captured[0].headers["Authorization"] == "Bearer access-token-old"

    async def test_expired_token_is_refreshed_and_persisted(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file, expired=True)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"items": []}]) as client:
            await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        token_request = captured[0]
        assert str(token_request.url) == TOKEN_URL
        form = dict(
            pair.split("=", 1) for pair in token_request.content.decode().split("&")
        )
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "refresh-token-1"
        assert captured[1].headers["Authorization"] == "Bearer access-token-new"
        stored = load_tokens(tokens_file)
        assert stored["access_token"] == "access-token-new"
        assert stored["refresh_token"] == "refresh-token-1"

    async def test_401_triggers_refresh_and_retry(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(
            captured, pages=[{"items": [TIMED_ITEM]}], reject_tokens={"access-token-old"}
        ) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )

        assert [event.uid for event in events] == ["evt-timed"]
        methods = [(request.method, str(request.url)) for request in captured]
        assert methods[1][1] == TOKEN_URL
        assert captured[2].headers["Authorization"] == "Bearer access-token-new"


class TestTokenStorage:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        tokens = write_tokens(path)
        assert load_tokens(path) == tokens

    def test_token_file_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        write_tokens(path)
        assert json.loads(path.read_text(encoding="utf-8"))["refresh_token"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    def test_token_file_is_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        write_tokens(path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_token_path_lives_in_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert token_path(3) == tmp_path / "google_token_3.json"
