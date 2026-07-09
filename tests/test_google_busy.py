"""Tests for the Google Calendar write client (Busy MV blocks)."""

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.google_busy import (
    BUSY_TITLE,
    MARKER_KEY,
    OWNER_KEY,
    OWNER_VALUE,
    BusyWriteClient,
    BusyWriteError,
    busy_write_token_path,
    event_body,
    has_write_token,
)
from app.models import CalendarEvent
from app.sources.google import save_tokens

BERLIN = ZoneInfo("Europe/Berlin")
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def write_write_token(path: Path, *, expired: bool = False) -> None:
    from datetime import timedelta

    expires_at = datetime.now(UTC) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    save_tokens(
        path,
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token-write",
            "access_token": "write-token-old",
            "access_token_expires_at": expires_at.isoformat(),
        },
    )


def timed_event(uid: str = "uid-1") -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title="MoreValue-Meeting",
        start=datetime(2026, 7, 10, 15, 30, tzinfo=BERLIN),
        end=datetime(2026, 7, 10, 16, 30, tzinfo=BERLIN),
        all_day=False,
        location="Büro",
    )


def all_day_event(uid: str = "uid-2") -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title="Ganztägig MV",
        start=date(2026, 7, 12),
        end=date(2026, 7, 13),
        all_day=True,
    )


class TestEventBody:
    def test_timed_block_is_neutral_and_marked(self) -> None:
        body = event_body("3|uid-1|2026-07-10", timed_event())
        assert body["summary"] == BUSY_TITLE
        assert body["transparency"] == "opaque"
        assert body["visibility"] == "private"
        private = body["extendedProperties"]["private"]
        assert private[MARKER_KEY] == "3|uid-1|2026-07-10"
        # Constant owner marker enables the exact-match reconciliation query.
        assert private[OWNER_KEY] == OWNER_VALUE
        # No appointment detail leaks: no description, no location, neutral title.
        assert "description" not in body
        assert "location" not in body
        assert "MoreValue" not in str(body)
        assert "Büro" not in str(body)
        # Timed → dateTime in UTC.
        assert body["start"]["dateTime"] == "2026-07-10T13:30:00+00:00"
        assert body["end"]["dateTime"] == "2026-07-10T14:30:00+00:00"

    def test_all_day_block_uses_dates(self) -> None:
        body = event_body("3|uid-2|2026-07-12", all_day_event())
        assert body["start"]["date"] == "2026-07-12"
        assert body["end"]["date"] == "2026-07-13"
        assert "dateTime" not in body["start"]


class TestHasWriteToken:
    def test_false_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert has_write_token() is False

    def test_true_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        write_write_token(busy_write_token_path())
        assert has_write_token() is True


def make_client(captured: list[httpx.Request], handler) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "write-token-new", "expires_in": 3599}
            )
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


@pytest.mark.anyio
class TestWriteOperations:
    async def test_insert_returns_event_id(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            return httpx.Response(200, json={"id": "gevt-created"})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            event_id = await client.insert_block("3|uid-1|2026-07-10", timed_event())
        assert event_id == "gevt-created"

    async def test_patch_targets_event_id(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "gevt-1"})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            await client.patch_block("gevt-1", "3|uid-1|2026-07-10", timed_event())
        patch_requests = [r for r in captured if r.method == "PATCH"]
        assert len(patch_requests) == 1
        assert patch_requests[0].url.path.endswith("/primary/events/gevt-1")

    async def test_delete_targets_event_id(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            await client.delete_block("gevt-1")
        delete_requests = [r for r in captured if r.method == "DELETE"]
        assert delete_requests[0].url.path.endswith("/primary/events/gevt-1")

    async def test_delete_treats_404_as_success(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": 404}})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            await client.delete_block("gevt-gone")  # must not raise

    async def test_insert_error_raises(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": 403}})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            with pytest.raises(BusyWriteError):
                await client.insert_block("3|uid-1|x", timed_event())


@pytest.mark.anyio
class TestListOwnBlocks:
    async def test_lists_only_marked_blocks_via_filter(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []
        marked = {
            "id": "gevt-1",
            "extendedProperties": {"private": {MARKER_KEY: "3|uid-1|x"}},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [marked]})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            blocks = await client.list_own_blocks()
        assert [b["id"] for b in blocks] == ["gevt-1"]
        # The request uses the constant owner-marker filter (exact match, no
        # wildcard) — never a full scan.
        list_req = next(r for r in captured if r.method == "GET")
        assert dict(list_req.url.params)["privateExtendedProperty"] == f"{OWNER_KEY}=1"

    async def test_unmarked_item_is_dropped_defensively(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []
        foreign = {"id": "foreign-evt", "summary": "Fremdtermin"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [foreign]})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            blocks = await client.list_own_blocks()
        # A foreign event that somehow lacks the marker is never returned.
        assert blocks == []


@pytest.mark.anyio
class TestTokenRefresh:
    async def test_expired_token_is_refreshed(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file, expired=True)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"id": "gevt-1"})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            await client.insert_block("3|uid-1|x", timed_event())
        assert str(captured[0].url) == TOKEN_URL
        insert = next(
            r for r in captured if r.method == "POST" and str(r.url) != TOKEN_URL
        )
        assert insert.headers["Authorization"] == "Bearer write-token-new"

    async def test_401_triggers_refresh_and_retry(self, tmp_path: Path) -> None:
        token_file = tmp_path / "w.json"
        write_write_token(token_file)
        captured: list[httpx.Request] = []
        state = {"rejected": False}

        def handler(request: httpx.Request) -> httpx.Response:
            token = request.headers.get("Authorization", "")
            if "write-token-old" in token and not state["rejected"]:
                state["rejected"] = True
                return httpx.Response(401, json={"error": {"code": 401}})
            return httpx.Response(200, json={"id": "gevt-1"})

        async with make_client(captured, handler) as http:
            client = BusyWriteClient(token_file, http)
            event_id = await client.insert_block("3|uid-1|x", timed_event())
        assert event_id == "gevt-1"
        assert any(str(r.url) == TOKEN_URL for r in captured)
