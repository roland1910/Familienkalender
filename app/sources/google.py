"""Google Calendar client (Calendar API v3 over plain REST).

Uses httpx directly instead of google-api-python-client: we need exactly
one API call (events.list with singleEvents=true, which expands recurring
events server side) plus the OAuth2 refresh-token grant. The official
client library would add a large dependency tree for no benefit.

Tokens (client_id, client_secret, refresh_token, access_token) live in a
JSON file under DATA_DIR with owner-only permissions — never in the HA
add-on options and never in the repository. The interactive OAuth consent
flow that produces the refresh token arrives with the admin UI stage;
this module only consumes existing tokens and keeps the short-lived
access token fresh.

Source config keys: ``calendar_id``.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.models import CalendarEvent
from app.sources import limits
from app.storage import resolve_data_dir

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL_TEMPLATE = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

# Refresh slightly early so a token never expires mid-request.
_EXPIRY_MARGIN = timedelta(seconds=60)

# 20 pages x 2500 maxResults = 50000 items — far beyond any family
# calendar (realistically < 2000 events in the sync window). More pages
# mean a broken or hostile server keeping us in the pagination loop.
MAX_PAGES = 20


def token_path(source_id: int) -> Path:
    """Where the OAuth tokens for a Google source are stored."""
    return resolve_data_dir() / f"google_token_{source_id}.json"


def load_tokens(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(path: Path, tokens: dict[str, Any]) -> None:
    """Persist tokens with owner-only file permissions (chmod 600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _is_expired(tokens: dict[str, Any]) -> bool:
    if not tokens.get("access_token"):
        return True
    raw = tokens.get("access_token_expires_at")
    if not raw:
        return True
    return datetime.now(UTC) >= datetime.fromisoformat(raw) - _EXPIRY_MARGIN


async def _refresh_access_token(
    tokens: dict[str, Any], token_file: Path, client: httpx.AsyncClient
) -> dict[str, Any]:
    """Exchange the refresh token for a fresh access token and persist it."""
    response = await client.post(
        TOKEN_URL,
        data={
            "client_id": tokens["client_id"],
            "client_secret": tokens["client_secret"],
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    payload = response.json()
    expires_at = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    tokens = {
        **tokens,
        "access_token": payload["access_token"],
        "access_token_expires_at": expires_at.isoformat(),
    }
    save_tokens(token_file, tokens)
    return tokens


def _item_to_event(item: dict[str, Any]) -> CalendarEvent:
    all_day = "date" in item["start"]
    if all_day:
        start: datetime | date = date.fromisoformat(item["start"]["date"])
        end: datetime | date = date.fromisoformat(item["end"]["date"])
    else:
        start = datetime.fromisoformat(item["start"]["dateTime"])
        end = datetime.fromisoformat(item["end"]["dateTime"])
    return CalendarEvent(
        uid=item["id"],
        title=item.get("summary", ""),
        start=start,
        end=end,
        all_day=all_day,
        location=item.get("location"),
    )


async def _list_events_page(
    calendar_id: str,
    access_token: str,
    window_start: datetime,
    window_end: datetime,
    page_token: str | None,
    client: httpx.AsyncClient,
) -> tuple[httpx.Response, bytes]:
    params: dict[str, str] = {
        "singleEvents": "true",
        "timeMin": window_start.astimezone(UTC).isoformat(),
        "timeMax": window_end.astimezone(UTC).isoformat(),
        "maxResults": "2500",
    }
    if page_token:
        params["pageToken"] = page_token
    url = EVENTS_URL_TEMPLATE.format(calendar_id=quote(calendar_id, safe=""))
    request = client.build_request(
        "GET", url, params=params, headers={"Authorization": f"Bearer {access_token}"}
    )
    return await limits.send_limited(client, request)


async def fetch_events(
    config: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    *,
    token_file: Path,
    client: httpx.AsyncClient | None = None,
) -> list[CalendarEvent]:
    """Fetch all events in [window_start, window_end).

    singleEvents=true makes the API expand recurring events server side.
    The access token is refreshed proactively when expired and once more
    reactively if the API still answers 401.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await fetch_events(
                config, window_start, window_end, token_file=token_file, client=own_client
            )

    tokens = load_tokens(token_file)
    if _is_expired(tokens):
        tokens = await _refresh_access_token(tokens, token_file, client)

    calendar_id = config["calendar_id"]
    events: list[CalendarEvent] = []
    page_token: str | None = None
    refreshed_after_401 = False
    pages_fetched = 0
    while True:
        if pages_fetched >= MAX_PAGES:
            raise limits.SyncLimitExceededError(
                f"Google-Kalender liefert mehr als {MAX_PAGES} Ergebnisseiten"
                " — Abbruch (Schutzlimit)"
            )
        response, body = await _list_events_page(
            calendar_id, tokens["access_token"], window_start, window_end, page_token, client
        )
        if response.status_code == 401 and not refreshed_after_401:
            refreshed_after_401 = True
            tokens = await _refresh_access_token(tokens, token_file, client)
            continue
        response.raise_for_status()
        pages_fetched += 1
        payload = json.loads(body)
        events.extend(
            _item_to_event(item)
            for item in payload.get("items", [])
            if item.get("status") != "cancelled"
        )
        limits.check_event_count(len(events))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return events
