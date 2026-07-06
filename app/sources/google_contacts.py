"""Google People API client: contact birthdays as recurring all-day events.

The Google "Birthdays" calendar is not exposed through the Calendar API, so
contact birthdays are fetched from the People API instead
(``people.connections.list`` with ``personFields=names,birthdays``). Each
contact birthday becomes one all-day CalendarEvent per year that falls into
the sync window, so the existing storage, filter and feed layers handle it
unchanged.

Design decisions:

- **Title format:** ``🎂 <Name>``. The cake emoji marks it visually as a
  birthday; the plain name follows. No age is derived even when the birth
  year is known — mixing "with age" and "without age" entries (many
  contacts have no year) would look inconsistent, and showing ages of
  family/friends on a shared kiosk is not wanted.
- **Missing year:** birthdays frequently carry only month/day. Those are
  handled exactly like dated ones, just without a year in the title (which
  it never has anyway).
- **29 February:** in non-leap years the birthday is shown on 28 February
  (the last valid day of the month) so it is never silently dropped; in
  leap years it stays on the 29th.

Token handling (storage, proactive refresh, one reactive 401 retry) and the
protective response/size/page limits are reused from ``app.sources.google``
and ``app.sources.limits`` so this client behaves like the Calendar one.

Source config carries no keys — the connected account defines whose
contacts are read.
"""

import calendar
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.models import CalendarEvent
from app.sources import limits
from app.sources.google import (
    MAX_PAGES,
    _is_expired,
    _refresh_access_token,
    load_tokens,
)

logger = logging.getLogger(__name__)

CONNECTIONS_URL = "https://people.googleapis.com/v1/people/me/connections"

# Title prefix marking an entry as a birthday.
BIRTHDAY_PREFIX = "🎂 "

# All-day events use an exclusive end date (iCalendar semantics): a one-day
# birthday on 2026-09-15 has end 2026-09-16.
_ONE_DAY = timedelta(days=1)


def _person_name(item: dict[str, Any]) -> str | None:
    """The contact's display name, or None if the person has no name."""
    for name in item.get("names", []):
        display = name.get("displayName")
        if display:
            return str(display)
    return None


def _person_birthday(item: dict[str, Any]) -> tuple[int, int, int | None] | None:
    """(month, day, year|None) for the person, or None if unusable.

    People API birthdays carry a structured ``date`` (month/day, optional
    year) and/or a free-text ``text`` field; only the structured date is
    used. Month/day out of range are treated as "no usable birthday".
    """
    for birthday in item.get("birthdays", []):
        part = birthday.get("date")
        if not isinstance(part, dict):
            continue
        month = part.get("month")
        day = part.get("day")
        if not isinstance(month, int) or not isinstance(day, int):
            continue
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year = part.get("year")
        year = year if isinstance(year, int) else None
        return (month, day, year)
    return None


def _occurrence_in_year(month: int, day: int, year: int) -> date | None:
    """The birthday's date in ``year``, clamping 29 Feb to the 28th if needed.

    Returns None only for genuinely impossible month/day combinations (e.g.
    31 April), which are then skipped for that year.
    """
    last_day = calendar.monthrange(year, month)[1]
    if day > last_day:
        # 29 Feb in a non-leap year → 28 Feb. Any other overflow (e.g. a
        # bogus 31st in a 30-day month) is clamped to the month's last day.
        day = last_day
    try:
        return date(year, month, day)
    except ValueError:
        return None


def birthday_events(
    name: str,
    *,
    month: int,
    day: int,
    year: int | None,  # noqa: ARG001 — part of the birthday contract, deliberately not rendered (no age shown)
    resource: str,
    window_start: datetime,
    window_end: datetime,
) -> list[CalendarEvent]:
    """All-day birthday events for one contact within [window_start, window_end).

    One event per calendar year the birthday falls into. UIDs are stable per
    (contact resource, year) so the feed and storage see updates as updates.
    The birth ``year`` is accepted (the People API supplies it when known)
    but intentionally not used: no age is derived or shown (see module doc).
    """
    title = f"{BIRTHDAY_PREFIX}{name}"
    events: list[CalendarEvent] = []
    for occ_year in range(window_start.year, window_end.year + 1):
        occurrence = _occurrence_in_year(month, day, occ_year)
        if occurrence is None:
            continue
        if not (window_start.date() <= occurrence < window_end.date()):
            continue
        events.append(
            CalendarEvent(
                uid=f"{resource}|{occ_year}",
                title=title,
                start=occurrence,
                end=occurrence + _ONE_DAY,
                all_day=True,
            )
        )
    return events


def _events_for_person(
    item: dict[str, Any], window_start: datetime, window_end: datetime
) -> list[CalendarEvent]:
    """Birthday events for one connection, or [] if it has no usable data."""
    name = _person_name(item)
    if not name:
        return []
    birthday = _person_birthday(item)
    if birthday is None:
        return []
    month, day, year = birthday
    resource = item.get("resourceName") or f"person|{name}"
    return birthday_events(
        name,
        month=month,
        day=day,
        year=year,
        resource=str(resource),
        window_start=window_start,
        window_end=window_end,
    )


async def _list_connections_page(
    access_token: str,
    page_token: str | None,
    client: httpx.AsyncClient,
) -> tuple[httpx.Response, bytes]:
    params: dict[str, str] = {
        "personFields": "names,birthdays",
        "pageSize": "1000",
    }
    if page_token:
        params["pageToken"] = page_token
    request = client.build_request(
        "GET",
        CONNECTIONS_URL,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
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
    """Fetch all contact birthdays as all-day events in the given window.

    The access token is refreshed proactively when expired and once more
    reactively if the API still answers 401. Malformed connections are
    skipped without aborting the whole fetch.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await fetch_events(
                config, window_start, window_end, token_file=token_file, client=own_client
            )

    tokens = load_tokens(token_file)
    if _is_expired(tokens):
        tokens = await _refresh_access_token(tokens, token_file, client)

    events: list[CalendarEvent] = []
    page_token: str | None = None
    refreshed_after_401 = False
    pages_fetched = 0
    skipped_items = 0
    while True:
        if pages_fetched >= MAX_PAGES:
            raise limits.SyncLimitExceededError(
                f"Google-Kontakte liefern mehr als {MAX_PAGES} Ergebnisseiten"
                " — Abbruch (Schutzlimit)"
            )
        response, body = await _list_connections_page(
            tokens["access_token"], page_token, client
        )
        if response.status_code == 401 and not refreshed_after_401:
            refreshed_after_401 = True
            tokens = await _refresh_access_token(tokens, token_file, client)
            continue
        response.raise_for_status()
        pages_fetched += 1
        payload = json.loads(body)
        for item in payload.get("connections", []):
            try:
                events.extend(_events_for_person(item, window_start, window_end))
            except (KeyError, ValueError, TypeError):
                skipped_items += 1
        limits.check_event_count(len(events))
        page_token = payload.get("nextPageToken")
        if not page_token:
            if skipped_items:
                # Count only — connection contents are foreign, untrusted data.
                logger.warning("Skipped %d malformed contact(s)", skipped_items)
            return events
