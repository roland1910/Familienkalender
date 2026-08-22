"""Protective limits for data fetched from external calendar servers.

The sync engine processes data it does not control (foreign invitations,
misbehaving or compromised servers). These caps bound memory and storage
so a single hostile or broken source cannot take the add-on down; hitting
a cap aborts the fetch with a clear error that ends up in last_sync_error.
"""

import dataclasses

import httpx

from app.models import CalendarEvent

# A family calendar realistically holds well under 2000 events in the
# ~97-day sync window; 10000 expanded occurrences only happen with an
# RRULE bomb or a grossly misconfigured source — abort instead of
# ballooning memory and the SQLite database.
MAX_EVENTS_PER_SOURCE = 10_000

# CalDAV multistatus / Google JSON responses for a family calendar are a
# few hundred KB at most; 10 MB bounds memory when a server misbehaves.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Titles and locations come from foreign calendars and invitations; 1000
# characters cover any legitimate value while bounding database growth
# and API payload size. The frontend additionally caps what it displays.
MAX_TEXT_LENGTH = 1000

# The description and the attendee list legitimately run longer than a title
# (a meeting body, a department-wide invitation), so they get their own cap
# instead of being cut at 1000 characters. Still a hard bound: the text is
# foreign, it is stored per event AND copied into the mirrored CalDAV
# resource, so an unbounded value would hit the database twice over.
MAX_LONG_TEXT_LENGTH = 4000


class SyncLimitExceededError(Exception):
    """A protective limit was exceeded while fetching a source."""


def _clip(value: str | None, limit: int) -> str | None:
    return value[:limit] if value else value


def clamp_event_text(event: CalendarEvent) -> CalendarEvent:
    """Truncate every foreign text field of an event to its limit.

    Title, location and organizer share MAX_TEXT_LENGTH; the description and
    the attendee list use the longer MAX_LONG_TEXT_LENGTH (see above).
    """
    clamped = dataclasses.replace(
        event,
        title=event.title[:MAX_TEXT_LENGTH],
        location=_clip(event.location, MAX_TEXT_LENGTH),
        organizer=_clip(event.organizer, MAX_TEXT_LENGTH),
        description=_clip(event.description, MAX_LONG_TEXT_LENGTH),
        attendees=_clip(event.attendees, MAX_LONG_TEXT_LENGTH),
    )
    # Returning the original when nothing changed keeps the common case
    # allocation-free (and identity-stable for callers comparing objects).
    return event if clamped == event else clamped


def check_event_count(count: int) -> None:
    """Abort once a source has produced more expanded events than allowed."""
    if count > MAX_EVENTS_PER_SOURCE:
        raise SyncLimitExceededError(
            f"Quelle liefert mehr als {MAX_EVENTS_PER_SOURCE} Termine"
            " im Sync-Fenster — Abbruch (Schutzlimit)"
        )


async def send_limited(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    auth: tuple[str, str] | None = None,
) -> tuple[httpx.Response, bytes]:
    """Send a request and read the response body with a hard size limit.

    Checks the declared Content-Length first, then counts the streamed
    bytes (a lying or absent Content-Length must not bypass the limit).
    Returns the response (for status/headers) and the body bytes.
    """
    limit_message = (
        f"Antwort des Kalender-Servers größer als {MAX_RESPONSE_BYTES} Bytes"
        " — Abbruch (Schutzlimit)"
    )
    kwargs: dict = {"stream": True}
    if auth is not None:
        kwargs["auth"] = auth
    response = await client.send(request, **kwargs)
    try:
        declared = response.headers.get("Content-Length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise SyncLimitExceededError(limit_message)
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise SyncLimitExceededError(limit_message)
            chunks.append(chunk)
        return response, b"".join(chunks)
    finally:
        await response.aclose()
