"""Google Calendar write client for the add-on's outgoing syncs into Xalt.

This is the ONLY module in the project that writes to an external Google
calendar. Two syncs use it, both writing into Roland's *primary* Xalt
calendar:

- the "Busy MV" sync (app.busy_sync): neutral busy blocks so his colleagues
  see in free/busy when a MoreValue appointment makes him unavailable,
  without exposing any appointment detail;
- the birthday sync (app.birthday_sync): one yearly all-day series per
  contact birthday.

Hard security invariant (enforced here and covered by tests): the add-on
touches ONLY its own, marked events, and each sync only its OWN kind. Every
event carries

    extendedProperties.private = {
        "familienkalender_owner": "<purpose>",   # "1" busy, "birthday"
        "familienkalender_busy"|"familienkalender_birthday": "<key>",
    }

and every update/delete targets an event id the add-on itself created (from
the persisted mapping). Reconciliation lists ONLY the own events of ONE
purpose via ``events.list?privateExtendedProperty=familienkalender_owner=…``
— Google matches that filter EXACTLY, so the busy sync never sees a birthday
series and vice versa, and neither ever sees a foreign event.

Least privilege: the flow uses the calendar.events scope on a SEPARATE
token file (``google_busywrite_token.json``); the read-only calendar/contacts
tokens are never reused for writes and vice versa.

Token handling (storage, proactive refresh, one reactive 401 retry) and the
protective response-size limit are reused from ``app.sources.google`` /
``app.sources.limits`` so this client behaves like the read one.
"""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.models import BUSY_BLOCK_TITLE, LOCAL_TZ, CalendarEvent
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

# Private marker key of the birthday series (value: the person key). A key of
# its own, so the busy sync's defence-in-depth check drops a birthday series
# even in the impossible case that the server-side filter returned one.
BIRTHDAY_MARKER_KEY = "familienkalender_birthday"

# Second, constant-valued marker on every event the add-on writes. Google's
# privateExtendedProperty filter does exact key=value matching only (no
# wildcards), so reconciliation cannot list "any familienkalender_busy value".
# This property gives a single exact query per purpose that returns ALL and
# ONLY that sync's own events — never a full calendar scan, never a foreign
# event, and never the other sync's events. The VALUE is the purpose tag:
# "1" is the historical busy value (kept so the ~85 existing blocks in
# Roland's calendar stay addressable), "birthday" is the birthday series.
OWNER_KEY = "familienkalender_owner"
OWNER_VALUE = "1"
OWNER_VALUE_BIRTHDAY = "birthday"

# Fixed, neutral title of every block — no appointment detail leaks. Single
# source of truth in app.models so the read clients skip the same title.
BUSY_TITLE = BUSY_BLOCK_TITLE

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
    (``transparency: opaque``). Visibility is Google's ``default`` — the title
    is already neutral and no detail leaks, so the block is shown as a normal
    calendar entry rather than flagged "private" (Roland's request). The
    private marker carries the source key so the block is self-identifying.
    Timed events use dateTime in UTC; all-day events use the exclusive
    date range (iCalendar semantics), exactly as stored.
    """
    body: dict[str, Any] = {
        "summary": BUSY_TITLE,
        "transparency": "opaque",
        "visibility": "default",
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


def birthday_event_body(person_key: str, title: str, start: date) -> dict[str, Any]:
    """The Google event body for one yearly birthday series.

    An all-day event on ``start`` with ``RRULE:FREQ=YEARLY`` — birthdays
    recur by nature, so ONE series per person replaces a fresh copy every
    year (and the series keeps working without the add-on writing again).

    ``transparency: transparent`` on purpose: a birthday must not mark
    Roland busy for a whole day in his colleagues' free/busy view. No
    description and no location are set (the title is all there is), and no
    age is derived — the same decision the source makes (see
    app.sources.google_contacts).
    """
    return {
        "summary": title,
        "transparency": "transparent",
        "visibility": "default",
        "start": {"date": start.isoformat()},
        "end": {"date": (start + timedelta(days=1)).isoformat()},
        "recurrence": ["RRULE:FREQ=YEARLY"],
        "extendedProperties": {
            "private": {
                BIRTHDAY_MARKER_KEY: person_key,
                OWNER_KEY: OWNER_VALUE_BIRTHDAY,
            }
        },
    }


def _date_str(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).date().isoformat()
    return value.isoformat()


def _utc_iso(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    # Defensive only: event_body branches on event.all_day BEFORE calling
    # _utc_iso, so a plain date never actually reaches this function today
    # (the date branch there calls _date_str instead). Kept as a safety net
    # in case that invariant ever changes, rather than raising here.
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

    async def list_marked_events(
        self, query_key: str, query_value: str, *, require_key: str
    ) -> list[dict[str, Any]]:
        """List own events by an EXACT private-marker match.

        ``query_key``/``query_value`` become the server-side
        ``privateExtendedProperty`` filter — Google matches it exactly (no
        wildcards), so exactly one purpose's events come back and never a
        foreign one. ``require_key`` is the defence-in-depth check applied to
        every returned item: it must carry that marker key as a string, which
        is what keeps the two syncs apart even if the server ever answered
        more loosely than asked.

        Returns raw Google event dicts (id, extendedProperties, times,
        visibility, recurrence) — the callers read what they need.
        """
        results: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            params = {
                "privateExtendedProperty": f"{query_key}={query_value}",
                "showDeleted": "false",
                "maxResults": "2500",
            }
            if page_token:
                params["pageToken"] = page_token
            response, body = await self._send("GET", _EVENTS_URL, params=params)
            if response.status_code != 200:
                raise BusyWriteError(
                    f"Eigene Termine konnten nicht gelesen werden (HTTP {response.status_code})."
                )
            payload = json.loads(body)
            for item in payload.get("items", []):
                if _marker_value(item, require_key) is not None and item.get("id"):
                    results.append(item)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return results
        raise BusyWriteError("Eigene Termine: zu viele Ergebnisseiten (Schutzlimit).")

    async def list_own_blocks(self, source_key: str | None = None) -> list[dict[str, Any]]:
        """List the add-on's own "Busy MV" blocks via the private marker(s).

        With ``source_key`` given, filters on that exact source key
        (``familienkalender_busy=<key>``); otherwise on the owner marker with
        the busy purpose (``familienkalender_owner=1``), which is what lists
        ALL busy blocks in one exact query. Reconciliation uses each item's
        ``visibility`` to detect and correct old blocks that were written as
        "private" (Google omits the field when it equals "default", so a
        missing value means default).
        """
        if source_key is not None:
            return await self.list_marked_events(
                MARKER_KEY, source_key, require_key=MARKER_KEY
            )
        return await self.list_marked_events(
            OWNER_KEY, OWNER_VALUE, require_key=MARKER_KEY
        )

    async def list_own_birthdays(self) -> list[dict[str, Any]]:
        """List the add-on's own yearly birthday series (exact owner match)."""
        return await self.list_marked_events(
            OWNER_KEY, OWNER_VALUE_BIRTHDAY, require_key=BIRTHDAY_MARKER_KEY
        )

    async def insert_event(self, body: dict[str, Any]) -> str:
        """Create a new own event from a prepared body; returns its event id."""
        response, payload = await self._send("POST", _EVENTS_URL, json_body=body)
        if response.status_code not in (200, 201):
            raise BusyWriteError(
                f"Termin konnte nicht angelegt werden (HTTP {response.status_code})."
            )
        return json.loads(payload)["id"]

    async def patch_event(self, google_event_id: str, body: dict[str, Any]) -> None:
        """Update an existing own event (PATCH by event id).

        The event id always comes from the persisted mapping or from a
        marker-filtered listing, i.e. it is always an event the add-on
        created — never a foreign one.
        """
        response, _ = await self._send(
            "PATCH", f"{_EVENTS_URL}/{google_event_id}", json_body=body
        )
        if response.status_code not in (200, 201):
            raise BusyWriteError(
                f"Termin konnte nicht aktualisiert werden (HTTP {response.status_code})."
            )

    async def delete_event(self, google_event_id: str) -> None:
        """Delete an own event by id. Already-gone (404/410) counts as success."""
        response, _ = await self._send("DELETE", f"{_EVENTS_URL}/{google_event_id}")
        if response.status_code in (200, 204, 404, 410):
            return
        raise BusyWriteError(
            f"Termin konnte nicht gelöscht werden (HTTP {response.status_code})."
        )

    async def insert_block(self, source_key: str, event: CalendarEvent) -> str:
        """Create a new "Busy MV" block; returns its Google event id."""
        return await self.insert_event(event_body(source_key, event))

    async def patch_block(
        self, google_event_id: str, source_key: str, event: CalendarEvent
    ) -> None:
        """Update the times of an existing own block (PATCH by event id)."""
        await self.patch_event(google_event_id, event_body(source_key, event))

    async def delete_block(self, google_event_id: str) -> None:
        """Delete an own block by event id (from the mapping)."""
        await self.delete_event(google_event_id)


def _marker_value(item: dict[str, Any], key: str) -> str | None:
    """The private-marker value under ``key``, or None if the item lacks it."""
    private = (item.get("extendedProperties") or {}).get("private") or {}
    if not isinstance(private, dict):
        return None
    value = private.get(key)
    return value if isinstance(value, str) else None
