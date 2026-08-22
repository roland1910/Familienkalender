"""Tests for the Google Calendar client (REST via httpx, mocked transport)."""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.sources import limits
from app.sources.google import MAX_PAGES, fetch_events, load_tokens, save_tokens, token_path

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

    async def test_skips_own_busy_blocks_by_marker(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        busy_block = {
            "id": "gevt-busy",
            "status": "confirmed",
            "summary": "Busy MV",
            "extendedProperties": {"private": {"familienkalender_busy": "3|uid|x"}},
            "start": {"dateTime": "2026-07-11T10:00:00+02:00"},
            "end": {"dateTime": "2026-07-11T11:00:00+02:00"},
        }
        pages = [{"items": [busy_block, TIMED_ITEM]}]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        # The self-created block is skipped; the normal event survives.
        assert [event.uid for event in events] == ["evt-timed"]

    async def test_skips_own_busy_blocks_by_title_fallback(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        # Marker stripped, but the fixed title still identifies it.
        busy_block = {
            "id": "gevt-busy-2",
            "status": "confirmed",
            "summary": "Busy MV",
            "start": {"dateTime": "2026-07-11T10:00:00+02:00"},
            "end": {"dateTime": "2026-07-11T11:00:00+02:00"},
        }
        pages = [{"items": [busy_block, TIMED_ITEM]}]
        async with make_client(captured, pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert [event.uid for event in events] == ["evt-timed"]

    async def test_skips_own_birthday_series(self, tmp_path: Path) -> None:
        """A birthday series the add-on wrote is never read back.

        Otherwise it would show up twice (next to the contact source) and the
        mirror sync would copy it on into MoreValue — where the birthday sync
        already maintains its own series.
        """
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        series = {
            "id": "gevt-bday",
            "status": "confirmed",
            "summary": "🎂 Oma",
            "extendedProperties": {
                "private": {
                    "familienkalender_birthday": "6|people/c1",
                    "familienkalender_owner": "birthday",
                }
            },
            "start": {"date": "2026-08-20"},
            "end": {"date": "2026-08-21"},
        }
        pages = [{"items": [series, TIMED_ITEM]}]
        async with make_client([], pages=pages) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert [event.uid for event in events] == ["evt-timed"]

    async def test_normal_event_with_other_extended_props_kept(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        # A foreign event carrying an unrelated private property must be kept.
        normal = {
            "id": "evt-normal",
            "status": "confirmed",
            "summary": "Echtes Meeting",
            "extendedProperties": {"private": {"some_other_key": "value"}},
            "start": {"dateTime": "2026-07-11T10:00:00+02:00"},
            "end": {"dateTime": "2026-07-11T11:00:00+02:00"},
        }
        async with make_client(captured, pages=[{"items": [normal]}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        assert [event.uid for event in events] == ["evt-normal"]

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


def attendee_item(
    uid: str, *, attendees: list[dict] | None = None, hour: int = 9
) -> dict:
    """A confirmed timed event with an arbitrary attendee list."""
    item = {
        "id": uid,
        "status": "confirmed",
        "summary": "Serientermin",
        "start": {"dateTime": f"2026-07-13T{hour:02d}:00:00+02:00"},
        "end": {"dateTime": f"2026-07-13T{hour + 1:02d}:00:00+02:00"},
    }
    if attendees is not None:
        item["attendees"] = attendees
    return item


@pytest.mark.anyio
class TestDeclinedEvents:
    """Roland declining an invitation must remove it everywhere.

    An event he declined stays in Google (struck through) but is no longer an
    appointment of his — it must vanish from the views, the feed and, via the
    normal mirror diff, from the MoreValue copy.
    """

    async def _fetch(self, tmp_path: Path, items: list[dict]) -> list[str]:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        async with make_client([], pages=[{"items": items}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        return [event.uid for event in events]

    async def test_own_declined_attendee_skips_the_event(self, tmp_path: Path) -> None:
        item = attendee_item(
            "evt-declined",
            attendees=[
                {"email": "chef@example.com", "responseStatus": "accepted"},
                {
                    "email": "roland@example.com",
                    "self": True,
                    "responseStatus": "declined",
                },
            ],
        )
        assert await self._fetch(tmp_path, [item, TIMED_ITEM]) == ["evt-timed"]

    @pytest.mark.parametrize(
        "status", ["accepted", "tentative", "needsAction", "unknown-future-value"]
    )
    async def test_every_other_own_response_keeps_the_event(
        self, tmp_path: Path, status: str
    ) -> None:
        item = attendee_item(
            "evt-kept",
            attendees=[{"email": "r@x", "self": True, "responseStatus": status}],
        )
        assert await self._fetch(tmp_path, [item]) == ["evt-kept"]

    async def test_own_attendee_without_response_status_is_kept(
        self, tmp_path: Path
    ) -> None:
        item = attendee_item("evt-no-status", attendees=[{"email": "r@x", "self": True}])
        assert await self._fetch(tmp_path, [item]) == ["evt-no-status"]

    async def test_event_without_attendees_is_kept(self, tmp_path: Path) -> None:
        """Self-created appointments carry no attendee list at all."""
        assert await self._fetch(tmp_path, [attendee_item("evt-solo")]) == ["evt-solo"]

    async def test_someone_elses_decline_never_removes_the_event(
        self, tmp_path: Path
    ) -> None:
        """Only Roland's OWN entry (self=true) decides — a colleague's does not."""
        item = attendee_item(
            "evt-foreign-decline",
            attendees=[
                {"email": "kollege@example.com", "responseStatus": "declined"},
                {"email": "r@x", "self": True, "responseStatus": "accepted"},
            ],
        )
        assert await self._fetch(tmp_path, [item]) == ["evt-foreign-decline"]

    async def test_declined_without_self_flag_is_kept(self, tmp_path: Path) -> None:
        """No self entry means no statement about Roland — never filter."""
        item = attendee_item(
            "evt-no-self",
            attendees=[{"email": "kollege@example.com", "responseStatus": "declined"}],
        )
        assert await self._fetch(tmp_path, [item]) == ["evt-no-self"]

    async def test_malformed_attendee_list_does_not_filter(self, tmp_path: Path) -> None:
        item = attendee_item("evt-broken-attendees")
        item["attendees"] = "declined"  # not a list — foreign data may be anything
        assert await self._fetch(tmp_path, [item]) == ["evt-broken-attendees"]

    async def test_single_declined_occurrence_of_a_series(self, tmp_path: Path) -> None:
        """singleEvents=true expands a series; only the declined one goes.

        Google hands back each occurrence as its own item with its own
        attendee list, so declining one appointment of a series removes
        exactly that one.
        """
        accepted = {"email": "r@x", "self": True, "responseStatus": "accepted"}
        declined = {"email": "r@x", "self": True, "responseStatus": "declined"}
        items = [
            attendee_item("series_20260713T070000Z", attendees=[accepted], hour=9),
            attendee_item("series_20260720T070000Z", attendees=[declined], hour=10),
            attendee_item("series_20260727T070000Z", attendees=[accepted], hour=11),
        ]
        assert await self._fetch(tmp_path, items) == [
            "series_20260713T070000Z",
            "series_20260727T070000Z",
        ]


@pytest.mark.anyio
class TestEventDetails:
    """Description, organizer and attendees (Etappe 45, part B).

    Roland asked for them explicitly: "wäre es super wenn in dem MV kalender
    etwas mehr details zu den google terminen stehen würde. z.b. wer
    eingeladen hat und die teilnehmer und falls es im google termin eine
    beschreibung gibt sollte diese ebenfalls gesynct werden."
    """

    async def _fetch_one(self, tmp_path: Path, item: dict):
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        async with make_client([], pages=[{"items": [item]}]) as client:
            events = await fetch_events(
                CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
            )
        return events[0]

    async def test_description_organizer_and_attendees_are_read(
        self, tmp_path: Path
    ) -> None:
        item = {
            **TIMED_ITEM,
            "description": "Agenda:\n- Rückblick\n- Planung",
            "organizer": {"email": "chef@example.com", "displayName": "Chef Chefin"},
            "attendees": [
                {"email": "chef@example.com", "displayName": "Chef Chefin"},
                {"email": "roland@example.com", "self": True},
                {"email": "raum-2@example.com", "displayName": "Besprechungsraum 2"},
            ],
        }
        event = await self._fetch_one(tmp_path, item)
        assert event.description == "Agenda:\n- Rückblick\n- Planung"
        assert event.organizer == "Chef Chefin <chef@example.com>"
        assert event.attendees == (
            "Chef Chefin <chef@example.com>\n"
            "roland@example.com\n"
            "Besprechungsraum 2 <raum-2@example.com>"
        )

    async def test_missing_details_stay_empty(self, tmp_path: Path) -> None:
        event = await self._fetch_one(tmp_path, TIMED_ITEM)
        assert event.description is None
        assert event.organizer is None
        assert event.attendees is None

    async def test_organizer_without_a_display_name_uses_the_address(
        self, tmp_path: Path
    ) -> None:
        item = {**TIMED_ITEM, "organizer": {"email": "chef@example.com"}}
        event = await self._fetch_one(tmp_path, item)
        assert event.organizer == "chef@example.com"

    async def test_response_status_is_deliberately_not_included(
        self, tmp_path: Path
    ) -> None:
        """Statuses churn constantly; including them would rewrite the copy
        on every colleague's accept/decline. Roland asked for the people."""
        item = {
            **TIMED_ITEM,
            "attendees": [
                {"email": "a@example.com", "responseStatus": "accepted"},
                {"email": "b@example.com", "responseStatus": "declined"},
            ],
        }
        event = await self._fetch_one(tmp_path, item)
        assert event.attendees == "a@example.com\nb@example.com"

    async def test_unusable_entries_are_dropped_not_rendered_empty(
        self, tmp_path: Path
    ) -> None:
        item = {
            **TIMED_ITEM,
            "organizer": {"displayName": "   "},
            "attendees": [
                "kaputt",
                {},
                {"displayName": "  Nur Name  "},
                {"email": "b@example.com"},
            ],
        }
        event = await self._fetch_one(tmp_path, item)
        assert event.organizer is None
        assert event.attendees == "Nur Name\nb@example.com"

    async def test_non_string_description_is_ignored(self, tmp_path: Path) -> None:
        event = await self._fetch_one(tmp_path, {**TIMED_ITEM, "description": {"x": 1}})
        assert event.description is None

    async def test_blank_description_is_ignored(self, tmp_path: Path) -> None:
        event = await self._fetch_one(tmp_path, {**TIMED_ITEM, "description": "  \n "})
        assert event.description is None


@pytest.mark.anyio
class TestFetchLimits:
    async def test_pagination_is_capped(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"items": [], "nextPageToken": "more"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(limits.SyncLimitExceededError, match=str(MAX_PAGES)):
                await fetch_events(
                    CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
                )
        assert len(captured) == MAX_PAGES

    async def test_declared_content_length_over_limit_aborts(self, tmp_path: Path) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": str(limits.MAX_RESPONSE_BYTES + 1)},
                content=b"",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(limits.SyncLimitExceededError):
                await fetch_events(
                    CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
                )

    async def test_broken_item_does_not_abort_the_fetch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        broken_item = {"id": "evt-broken-geheim", "status": "confirmed", "start": {}}
        pages = [{"items": [TIMED_ITEM, broken_item, ALL_DAY_ITEM]}]
        async with make_client(captured, pages=pages) as client:
            with caplog.at_level("WARNING", logger="app.sources.google"):
                events = await fetch_events(
                    CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
                )

        # The two valid events survive the malformed item.
        assert [event.uid for event in events] == ["evt-timed", "evt-allday"]
        # The failure is counted and logged — without leaking raw event data.
        assert any("1" in record.getMessage() for record in caplog.records)
        assert all("geheim" not in record.getMessage() for record in caplog.records)

    async def test_event_cap_aborts_with_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.sources.limits.MAX_EVENTS_PER_SOURCE", 1)
        tokens_file = tmp_path / "tokens.json"
        write_tokens(tokens_file)
        captured: list[httpx.Request] = []
        pages = [{"items": [TIMED_ITEM, ALL_DAY_ITEM]}]
        async with make_client(captured, pages=pages) as client:
            with pytest.raises(limits.SyncLimitExceededError, match="1"):
                await fetch_events(
                    CONFIG, WINDOW_START, WINDOW_END, token_file=tokens_file, client=client
                )


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

    def test_overwriting_replaces_longer_content_completely(self, tmp_path: Path) -> None:
        path = tmp_path / "tokens.json"
        save_tokens(path, {"refresh_token": "x" * 500})
        save_tokens(path, {"refresh_token": "short"})
        assert load_tokens(path) == {"refresh_token": "short"}

    def test_token_path_lives_in_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert token_path(3) == tmp_path / "google_token_3.json"
