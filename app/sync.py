"""Sync orchestration: fetch all enabled sources and store their events.

Each source is synced independently — a broken source (server down, bad
credentials) must never block the others. Failures are recorded on the
source (last_sync_error) so the admin UI can surface them later.
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

from app.models import LOCAL_TZ, CalendarEvent, Source
from app.sanitize import sanitize_error
from app.sources import caldav, google, google_contacts, limits
from app.storage import Storage

logger = logging.getLogger(__name__)

SYNC_WINDOW_PAST_DAYS = 7
SYNC_WINDOW_FUTURE_DAYS = 90
DEFAULT_SYNC_INTERVAL_SECONDS = 300

# Serializes sync runs: the periodic task and manual POST /api/sync must
# never fetch and write concurrently (duplicate work, interleaved writes).
SYNC_LOCK = asyncio.Lock()


def sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The fetch window: local midnight 7 days back to 90 days ahead."""
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start_day = now_local.date() - timedelta(days=SYNC_WINDOW_PAST_DAYS)
    end_day = now_local.date() + timedelta(days=SYNC_WINDOW_FUTURE_DAYS)
    return (
        datetime.combine(start_day, time.min, tzinfo=LOCAL_TZ),
        datetime.combine(end_day, time.min, tzinfo=LOCAL_TZ),
    )


async def _fetch_source_events(
    source: Source, window_start: datetime, window_end: datetime
) -> list[CalendarEvent]:
    if source.type == "caldav":
        events = await caldav.fetch_events(source.config, window_start, window_end)
    elif source.type == "google":
        events = await google.fetch_events(
            source.config,
            window_start,
            window_end,
            token_file=google.token_path(source.id),
        )
    elif source.type == "google_contacts":
        # Contact birthdays via the People API — same token file layout as
        # a Google Calendar source (google_token_<source_id>.json).
        events = await google_contacts.fetch_events(
            source.config,
            window_start,
            window_end,
            token_file=google.token_path(source.id),
        )
    else:
        raise ValueError(f"unknown source type: {source.type!r}")
    # Applied here (not per client) so every current and future source type
    # gets the same server-side cap on foreign text lengths.
    return [limits.clamp_event_text(event) for event in events]


async def sync_all(storage: Storage, *, now: datetime | None = None) -> dict[int, str | None]:
    """Sync every enabled source; returns per-source error (None = success).

    Runs under SYNC_LOCK: concurrent callers wait until the running sync
    finishes (the API layer answers 409 instead of queueing up).
    """
    async with SYNC_LOCK:
        return await _sync_all_locked(storage, now=now)


async def _sync_all_locked(
    storage: Storage, *, now: datetime | None = None
) -> dict[int, str | None]:
    window_start, window_end = sync_window(now)
    results: dict[int, str | None] = {}
    # One consistent timestamp for the whole run, not one per source.
    synced_at = now or datetime.now(UTC)
    for source in storage.list_sources():
        if not source.enabled:
            continue
        try:
            events = await _fetch_source_events(source, window_start, window_end)
            storage.sync_events(
                source.id, events, window_start, window_end, synced_at=synced_at
            )
            storage.update_sync_status(source.id, synced_at=synced_at, error=None)
            results[source.id] = None
        except Exception as exc:
            error = sanitize_error(str(exc))
            logger.warning("Sync failed for source %s (%s): %s", source.id, source.name, error)
            storage.update_sync_status(source.id, synced_at=synced_at, error=error)
            results[source.id] = error
    return results


async def periodic_sync(storage: Storage, interval_seconds: float) -> None:
    """Run sync_all forever with the given interval (used as a lifespan task)."""
    while True:
        try:
            await sync_all(storage)
        except Exception:  # pragma: no cover - sync_all already isolates errors
            logger.exception("Unexpected error in periodic sync")
        await asyncio.sleep(interval_seconds)
