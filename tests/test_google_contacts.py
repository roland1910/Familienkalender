"""Tests for the Google People API birthdays client (REST via httpx, mocked)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.sources import limits
from app.sources.google import save_tokens
from app.sources.google_contacts import (
    CONNECTIONS_URL,
    birthday_events,
    fetch_events,
)

BERLIN = ZoneInfo("Europe/Berlin")

# A generous window that spans a full year so a birthday shows exactly once.
WINDOW_START = datetime(2026, 7, 1, tzinfo=BERLIN)
WINDOW_END = datetime(2027, 6, 30, tzinfo=BERLIN)

CONFIG: dict = {}

TOKEN_URL = "https://oauth2.googleapis.com/token"


def person(
    name: str | None,
    *,
    month: int | None = None,
    day: int | None = None,
    year: int | None = None,
    resource: str = "people/c1",
) -> dict:
    """Build a People API person resource with an optional birthday."""
    result: dict = {"resourceName": resource, "etag": "e"}
    if name is not None:
        result["names"] = [{"displayName": name}]
    if month is not None and day is not None:
        date_part: dict = {"month": month, "day": day}
        if year is not None:
            date_part["year"] = year
        result["birthdays"] = [{"date": date_part}]
    return result


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
    """Mock transport serving the token endpoint and paginated connections."""
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


class TestBirthdayEvents:
    """Pure event generation for one person (no HTTP)."""

    def test_birthday_with_year_shows_once_in_window(self) -> None:
        events = birthday_events(
            "Oma", month=9, day=15, year=1950, resource="people/c1",
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
        assert len(events) == 1
        event = events[0]
        assert event.all_day is True
        assert event.start == date(2026, 9, 15)
        assert event.end == date(2026, 9, 16)  # exclusive end (one day)
        assert "Oma" in event.title

    def test_birthday_without_year_still_generates(self) -> None:
        events = birthday_events(
            "Papa", month=3, day=20, year=None, resource="people/c2",
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
        assert len(events) == 1
        assert events[0].start == date(2027, 3, 20)

    def test_title_has_no_age_when_year_is_missing(self) -> None:
        events = birthday_events(
            "Papa", month=3, day=20, year=None, resource="people/c2",
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
        # No numeric age anywhere in the title when the birth year is unknown.
        assert not any(ch.isdigit() for ch in events[0].title)

    def test_repeats_across_a_multi_year_window(self) -> None:
        events = birthday_events(
            "Kind", month=1, day=10, year=2015, resource="people/c3",
            window_start=datetime(2026, 1, 1, tzinfo=BERLIN),
            window_end=datetime(2028, 6, 1, tzinfo=BERLIN),
        )
        assert [event.start for event in events] == [
            date(2026, 1, 10),
            date(2027, 1, 10),
            date(2028, 1, 10),
        ]

    def test_feb_29_maps_to_feb_28_in_non_leap_years(self) -> None:
        # 2026 and 2027 are not leap years; 2028 is.
        events = birthday_events(
            "Schaltkind", month=2, day=29, year=2000, resource="people/c4",
            window_start=datetime(2026, 1, 1, tzinfo=BERLIN),
            window_end=datetime(2028, 12, 1, tzinfo=BERLIN),
        )
        assert [event.start for event in events] == [
            date(2026, 2, 28),
            date(2027, 2, 28),
            date(2028, 2, 29),
        ]

    def test_stable_uid_per_person_and_year(self) -> None:
        first = birthday_events(
            "Oma", month=9, day=15, year=1950, resource="people/c1",
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
        again = birthday_events(
            "Oma", month=9, day=15, year=1950, resource="people/c1",
            window_start=WINDOW_START, window_end=WINDOW_END,
        )
        assert first[0].uid == again[0].uid
        # Different year in window → different uid.
        multi = birthday_events(
            "Oma", month=9, day=15, year=1950, resource="people/c1",
            window_start=datetime(2026, 1, 1, tzinfo=BERLIN),
            window_end=datetime(2028, 1, 1, tzinfo=BERLIN),
        )
        assert multi[0].uid != multi[1].uid


@pytest.mark.anyio
class TestFetchEvents:
    async def test_maps_people_with_birthdays(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [
            {
                "connections": [
                    person("Oma", month=9, day=15, year=1950),
                    person("Papa", month=3, day=20),
                ]
            }
        ]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        titles = sorted(event.title for event in events)
        assert any("Oma" in title for title in titles)
        assert any("Papa" in title for title in titles)
        assert all(event.all_day for event in events)

    async def test_requests_names_and_birthdays_fields(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"connections": []}]) as client:
            await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        request = captured[0]
        assert str(request.url).startswith(CONNECTIONS_URL)
        params = dict(request.url.params)
        assert params["personFields"] == "names,birthdays"
        assert params["pageSize"] == "1000"

    async def test_skips_person_without_name(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [
            {
                "connections": [
                    person(None, month=5, day=1, year=1980),
                    person("Mit Name", month=6, day=2),
                ]
            }
        ]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert all("Mit Name" in event.title for event in events)
        assert len(events) == 1

    async def test_skips_person_without_birthday(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [{"connections": [person("Ohne Geburtstag")]}]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert events == []

    async def test_follows_pagination(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [
            {
                "connections": [person("Erste", month=1, day=1, resource="people/a")],
                "nextPageToken": "page-2",
            },
            {"connections": [person("Zweite", month=2, day=2, resource="people/b")]},
        ]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        titles = {event.title for event in events}
        assert any("Erste" in title for title in titles)
        assert any("Zweite" in title for title in titles)
        assert dict(captured[1].url.params)["pageToken"] == "page-2"

    async def test_expired_token_is_refreshed(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file, expired=True)
        captured: list[httpx.Request] = []
        async with make_client(captured, pages=[{"connections": []}]) as client:
            await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert str(captured[0].url) == TOKEN_URL
        assert captured[1].headers["Authorization"] == "Bearer access-token-new"

    async def test_401_triggers_refresh_and_retry(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        async with make_client(
            captured,
            pages=[{"connections": [person("Oma", month=9, day=15)]}],
            reject_tokens={"access-token-old"},
        ) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert len(events) == 1
        assert str(captured[1].url) == TOKEN_URL
        assert captured[2].headers["Authorization"] == "Bearer access-token-new"

    async def test_broken_person_does_not_abort(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        broken = {"resourceName": "people/x", "birthdays": [{"date": {"month": 13}}]}
        pages = [
            {
                "connections": [
                    person("Gut", month=4, day=4, year=1990),
                    broken,
                ]
            }
        ]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert all("Gut" in event.title for event in events)

    async def test_pagination_is_capped(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"connections": [], "nextPageToken": "more"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(limits.SyncLimitExceededError):
                await fetch_events(
                    CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
                )
