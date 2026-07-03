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


class SyncLimitExceededError(Exception):
    """A protective limit was exceeded while fetching a source."""


def clamp_event_text(event: CalendarEvent) -> CalendarEvent:
    """Truncate title and location to MAX_TEXT_LENGTH characters."""
    title = event.title[:MAX_TEXT_LENGTH]
    location = event.location[:MAX_TEXT_LENGTH] if event.location else event.location
    if title == event.title and location == event.location:
        return event
    return dataclasses.replace(event, title=title, location=location)


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
