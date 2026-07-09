"""Google Calendar write client for the one-way "Busy MV" sync.

This is the ONLY module in the project that writes to an external calendar.
It maintains neutral "Busy MV" blocks in Roland's *primary* Xalt Google
calendar so his colleagues see (in free/busy) when a MoreValue appointment
makes him unavailable — without exposing any appointment detail.

Hard security invariant (enforced here and covered by tests): the add-on
touches ONLY its own, marked events. Every block it creates carries

    extendedProperties.private = {"familienkalender_busy": "<source-key>"}

and every update/delete targets an event id the add-on itself created (from
the persisted busy_blocks mapping). Reconciliation of orphaned blocks lists
ONLY the own blocks via ``events.list?privateExtendedProperty=...`` — never
a full calendar scan, so a foreign event is never even read, let alone
modified or deleted.

Least privilege: the flow uses the calendar.events scope on a SEPARATE
token file (``google_busywrite_token.json``); the read-only calendar/contacts
tokens are never reused for writes and vice versa.

Token handling (storage, proactive refresh, one reactive 401 retry) and the
protective response-size limit are reused from ``app.sources.google`` /
``app.sources.limits`` so this client behaves like the read one.
"""

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from app.models import LOCAL_TZ, CalendarEvent
from app.sources import limits
from app.sources.google import (
    _is_expired,
    _refresh_access_token,
    load_tokens,
)
from app.storage import resolve_data_dir

logger = logging.getLogger(__name__)

# The primary calendar of the connected (Xalt) account is the target; the
# id "primary" is Google's documented alias, so no calendar id is stored.
CALENDAR_ID = "primary"

_EVENTS_URL = f"https://www.googleapis.com/calendar/v3/calendars/{CALENDAR_ID}/events"

# Fixed private marker key on every block the add-on creates. Its value is
# the source key of the mirrored event, so a block is self-describing.
MARKER_KEY = "familienkalender_busy"

# Second, constant-valued marker on every block. Google's
# privateExtendedProperty filter does exact key=value matching only (no
# wildcards), so reconciliation cannot list "any familienkalender_busy value".
# This constant-valued property gives a single exact query that returns ALL
# and ONLY the add-on's own blocks — never a full calendar scan, never a
# foreign event.
OWNER_KEY = "familienkalender_owner"
OWNER_VALUE = "1"

# Fixed, neutral title of every block — no appointment detail leaks.
BUSY_TITLE = "Busy MV"

# events.list for the add-on's own blocks pages very rarely (only as many
# blocks as MoreValue appointments in the window); cap the loop defensively.
_MAX_LIST_PAGES = 20


class BusyWriteError(Exception):
    """A busy-sync write/list operation failed (message is sanitized upstream)."""


def busy_write_token_path() -> Path:
    """Where the separate calendar.events write tokens live (owner-only)."""
    return resolve_data_dir() / "google_busywrite_token.json"


def has_write_token() -> bool:
    """Whether a write token has been connected (file exists and parses)."""
    path = busy_write_token_path()
    if not path.exists():
        return False
    try:
        tokens = load_tokens(path)
    except (OSError, ValueError):
        return False
    return bool(tokens.get("refresh_token"))


def event_body(source_key: str, event: CalendarEvent) -> dict[str, Any]:
    """The Google event body for a "Busy MV" block mirroring ``event``.

    Neutral by design: fixed summary, no description/location, marked busy
    (``transparency: opaque``) and ``visibility: private``. The private
    marker carries the source key so the block is self-identifying.
    Timed events use dateTime in UTC; all-day events use the exclusive
    date range (iCalendar semantics), exactly as stored.
    """
    body: dict[str, Any] = {
        "summary": BUSY_TITLE,
        "transparency": "opaque",
        "visibility": "private",
        "extendedProperties": {
            "private": {MARKER_KEY: source_key, OWNER_KEY: OWNER_VALUE}
        },
    }
    if event.all_day:
        body["start"] = {"date": _date_str(event.start)}
        body["end"] = {"date": _date_str(event.end)}
    else:
        body["start"] = {"dateTime": _utc_iso(event.start)}
        body["end"] = {"dateTime": _utc_iso(event.end)}
    return body


def _date_str(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).date().isoformat()
    return value.isoformat()


def _utc_iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    # Defensive: an all-day value reaching the timed branch — treat as local
    # midnight in UTC rather than raising.
    return datetime.combine(value, datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(
        UTC
    ).isoformat()


class BusyWriteClient:
    """Thin authenticated wrapper around the Calendar events write endpoints.

    One instance per sync run; it owns the (refreshed) access token and the
    httpx client. Refreshes proactively when the token is expired and once
    reactively on a 401.
    """

    def __init__(self, token_file: Path, client: httpx.AsyncClient) -> None:
        self._token_file = token_file
        self._client = client
        self._tokens: dict[str, Any] | None = None

    async def _access_token(self) -> str:
        if self._tokens is None:
            tokens = load_tokens(self._token_file)
            if _is_expired(tokens):
                tokens = await _refresh_access_token(tokens, self._token_file, self._client)
            self._tokens = tokens
        return self._tokens["access_token"]

    async def _send(
        self, method: str, url: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> tuple[httpx.Response, bytes]:
        """Send an authenticated request, refreshing once on a 401."""
        for attempt in range(2):
            token = await self._access_token()
            request = self._client.build_request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            response, body = await limits.send_limited(self._client, request)
            if response.status_code == 401 and attempt == 0:
                # Force a refresh and retry once.
                refreshed = await _refresh_access_token(
                    self._tokens or load_tokens(self._token_file),
                    self._token_file,
                    self._client,
                )
                self._tokens = refreshed
                continue
            return response, body
        return response, body

    async def list_own_blocks(self, source_key: str | None = None) -> list[dict[str, Any]]:
        """List the add-on's own blocks via the private marker(s).

        With ``source_key`` given, filters on that exact source key
        (``familienkalender_busy=<key>``); otherwise filters on the constant
        owner marker (``familienkalender_owner=1``) — Google's
        privateExtendedProperty does exact matching only (no wildcards), so
        the constant-valued marker is what lets us list ALL own blocks in one
        query. Either way the filter is applied server-side, so ONLY the
        add-on's own blocks come back — never a foreign event. Returns raw
        Google event dicts (id + extendedProperties + times).
        """
        if source_key is not None:
            query = f"{MARKER_KEY}={source_key}"
        else:
            query = f"{OWNER_KEY}={OWNER_VALUE}"
        results: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            params = {
                "privateExtendedProperty": query,
                "showDeleted": "false",
                "maxResults": "2500",
            }
            if page_token:
                params["pageToken"] = page_token
            response, body = await self._send("GET", _EVENTS_URL, params=params)
            if response.status_code != 200:
                raise BusyWriteError(
                    f"Belegt-Blöcke konnten nicht gelesen werden (HTTP {response.status_code})."
                )
            payload = json.loads(body)
            for item in payload.get("items", []):
                # Defence in depth: only keep items that actually carry OUR
                # marker, even though the server filtered by it.
                if _own_marker(item) is not None and item.get("id"):
                    results.append(item)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return results
        raise BusyWriteError("Belegt-Blöcke: zu viele Ergebnisseiten (Schutzlimit).")

    async def insert_block(self, source_key: str, event: CalendarEvent) -> str:
        """Create a new "Busy MV" block; returns its Google event id."""
        response, body = await self._send(
            "POST", _EVENTS_URL, json_body=event_body(source_key, event)
        )
        if response.status_code not in (200, 201):
            raise BusyWriteError(
                f"Belegt-Block konnte nicht angelegt werden (HTTP {response.status_code})."
            )
        return json.loads(body)["id"]

    async def patch_block(
        self, google_event_id: str, source_key: str, event: CalendarEvent
    ) -> None:
        """Update the times of an existing own block (PATCH by event id).

        The event id comes from the persisted mapping, i.e. it is always a
        block the add-on created — never a foreign event.
        """
        url = f"{_EVENTS_URL}/{google_event_id}"
        response, _ = await self._send(
            "PATCH", url, json_body=event_body(source_key, event)
        )
        if response.status_code not in (200, 201):
            raise BusyWriteError(
                f"Belegt-Block konnte nicht aktualisiert werden (HTTP {response.status_code})."
            )

    async def delete_block(self, google_event_id: str) -> None:
        """Delete an own block by event id (from the mapping).

        A 404/410 (already gone) is treated as success — the goal state
        (block absent) is reached either way.
        """
        url = f"{_EVENTS_URL}/{google_event_id}"
        response, _ = await self._send("DELETE", url)
        if response.status_code in (200, 204, 404, 410):
            return
        raise BusyWriteError(
            f"Belegt-Block konnte nicht gelöscht werden (HTTP {response.status_code})."
        )


def _own_marker(item: dict[str, Any]) -> str | None:
    """The busy marker value of a Google event, or None if it is not ours."""
    private = (item.get("extendedProperties") or {}).get("private") or {}
    value = private.get(MARKER_KEY)
    return value if isinstance(value, str) else None
