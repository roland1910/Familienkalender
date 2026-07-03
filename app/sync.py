"""Sync orchestration: fetch all enabled sources and store their events.

Each source is synced independently — a broken source (server down, bad
credentials) must never block the others. Failures are recorded on the
source (last_sync_error) so the admin UI can surface them later.
"""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

from app.models import LOCAL_TZ, Source
from app.sources import caldav, google
from app.storage import Storage

logger = logging.getLogger(__name__)

SYNC_WINDOW_PAST_DAYS = 7
SYNC_WINDOW_FUTURE_DAYS = 90
DEFAULT_SYNC_INTERVAL_SECONDS = 300


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
) -> list:
    if source.type == "caldav":
        return await caldav.fetch_events(source.config, window_start, window_end)
    if source.type == "google":
        return await google.fetch_events(
            source.config,
            window_start,
            window_end,
            token_file=google.token_path(source.id),
        )
    raise ValueError(f"unknown source type: {source.type!r}")


async def sync_all(storage: Storage, *, now: datetime | None = None) -> dict[int, str | None]:
    """Sync every enabled source; returns per-source error (None = success)."""
    window_start, window_end = sync_window(now)
    results: dict[int, str | None] = {}
    for source in storage.list_sources():
        if not source.enabled:
            continue
        synced_at = now or datetime.now(UTC)
        try:
            events = await _fetch_source_events(source, window_start, window_end)
            storage.sync_events(
                source.id, events, window_start, window_end, synced_at=synced_at
            )
            storage.update_sync_status(source.id, synced_at=synced_at, error=None)
            results[source.id] = None
        except Exception as exc:
            logger.warning("Sync failed for source %s (%s): %s", source.id, source.name, exc)
            storage.update_sync_status(source.id, synced_at=synced_at, error=str(exc))
            results[source.id] = str(exc)
    return results


async def periodic_sync(storage: Storage, interval_seconds: float) -> None:
    """Run sync_all forever with the given interval (used as a lifespan task)."""
    while True:
        try:
            await sync_all(storage)
        except Exception:  # pragma: no cover - sync_all already isolates errors
            logger.exception("Unexpected error in periodic sync")
        await asyncio.sleep(interval_seconds)
