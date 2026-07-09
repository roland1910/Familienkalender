"""One-way "Busy MV" sync: mirror MoreValue appointments into Xalt.

For every appointment in the configured source(s) within [today, +180 days)
this maintains exactly one neutral "Busy MV" block in Roland's primary Xalt
Google calendar, so his colleagues see (in free/busy) when he is unavailable.
The sync is stateful and strictly one-way: it never writes back into the
source calendars and never touches any calendar entry it did not create.

Safety design (see also app.google_busy):

- Every block carries a private marker (source key). The persisted
  ``busy_blocks`` mapping (source key → Google event id) drives all
  update/delete operations, so a write always targets a self-created id.
- The reconciliation pass lists ONLY the add-on's own blocks (server-side
  ``privateExtendedProperty`` filter) — never a full calendar scan — and
  removes blocks whose source event has vanished, again by their own id.
- Errors are isolated: a busy-sync failure never blocks the normal calendar
  sync. The last-run outcome (with a sanitized error) is persisted for the
  admin UI.

The sync runs only when it is enabled AND a write token is connected.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app import settings
from app.google_busy import (
    BusyWriteClient,
    BusyWriteError,
    busy_write_token_path,
    has_write_token,
)
from app.models import LOCAL_TZ, BusyBlock, StoredEvent
from app.sanitize import sanitize_error
from app.storage import Storage, _encode_moment

logger = logging.getLogger(__name__)

# How far ahead MoreValue appointments are mirrored (Roland's choice).
BUSY_SYNC_FUTURE_DAYS = 180


def busy_sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The mirror window: local midnight today to +180 days.

    Past appointments are never mirrored — an appointment that already
    started is irrelevant for future free/busy.
    """
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=LOCAL_TZ)
    end = start + timedelta(days=BUSY_SYNC_FUTURE_DAYS)
    return start, end


def source_key(source_id: int, event) -> str:
    """Stable key for a source event, matching the events-table identity.

    ``source_id|uid|<encoded-start>`` — the encoded start uses the same
    encoding storage uses (UTC ISO for timed, ISO date for all-day), so the
    key is stable across syncs and independent of the process timezone.
    """
    return f"{source_id}|{event.uid}|{_encode_moment(event.start)}"


@dataclass(frozen=True)
class BusySyncResult:
    """Outcome of one busy-sync run (for logging/status)."""

    inserted: int
    updated: int
    deleted: int
    orphans_removed: int
    active_blocks: int
    error: str | None


def _desired_events(
    storage: Storage, source_ids: set[int], window_start: datetime, window_end: datetime
) -> dict[str, StoredEvent]:
    """Source events to mirror, keyed by source key.

    Only events from the selected sources whose start lies within
    [window_start, window_end) — all-day events included. Past events (start
    before window_start) are excluded so no blocks are created in the past.
    """
    desired: dict[str, StoredEvent] = {}
    for item in storage.get_events(window_start, window_end):
        if item.source_id not in source_ids:
            continue
        start = item.event.start_as_datetime()
        # storage.get_events returns everything OVERLAPPING the window,
        # including an event that started before window_start and merely
        # reaches into it. Only a start actually inside the window counts
        # for busy-sync (see the docstring above and busy_sync_window: past
        # appointments are never mirrored) — so this second, strict check on
        # the start alone is required and not redundant with get_events.
        if not (window_start <= start < window_end):
            continue
        desired[source_key(item.source_id, item.event)] = item
    return desired


def _times_differ(block: BusyBlock, item: StoredEvent) -> bool:
    """Whether a mapped block's stored time range no longer matches the event."""
    event = item.event
    return (
        block.all_day != event.all_day
        or _encode_moment(block.start) != _encode_moment(event.start)
        or _encode_moment(block.end) != _encode_moment(event.end)
    )


async def _reconcile(
    storage: Storage,
    client: BusyWriteClient,
    desired: dict[str, StoredEvent],
    mapping: dict[str, BusyBlock],
    now: datetime,
) -> BusySyncResult:
    inserted = updated = deleted = orphans = 0
    # Every Google event id the add-on knows about after this run — a block
    # is legitimate (not an orphan) iff it is still mapped to a desired event.
    # Built as we go so a freshly inserted block is not mistaken for an orphan.
    known_ids: set[str] = set()

    # 1. Insert / update from the desired set.
    for key, item in desired.items():
        existing = mapping.get(key)
        if existing is None:
            event_id = await client.insert_block(key, item.event)
            storage.upsert_busy_block(
                BusyBlock(key, event_id, item.event.start, item.event.end, item.event.all_day),
                updated_at=now,
            )
            known_ids.add(event_id)
            inserted += 1
        else:
            known_ids.add(existing.google_event_id)
            if _times_differ(existing, item):
                await client.patch_block(existing.google_event_id, key, item.event)
                storage.upsert_busy_block(
                    BusyBlock(
                        key,
                        existing.google_event_id,
                        item.event.start,
                        item.event.end,
                        item.event.all_day,
                    ),
                    updated_at=now,
                )
                updated += 1

    # 2. Delete mapped blocks whose source event is gone or out of window.
    for key, block in mapping.items():
        if key not in desired:
            await client.delete_block(block.google_event_id)
            storage.delete_busy_block(key)
            deleted += 1

    # 3. Reconcile orphans: own blocks in the calendar not backed by a
    #    still-desired mapping (a lost mapping row, a leftover from an earlier
    #    run). Listing is restricted to OUR marker, so no foreign event is
    #    ever touched. known_ids holds exactly the blocks that should remain.
    for own in await client.list_own_blocks():
        event_id = own.get("id")
        if event_id and event_id not in known_ids:
            await client.delete_block(event_id)
            orphans += 1

    active = storage.count_busy_blocks()
    return BusySyncResult(inserted, updated, deleted, orphans, active, None)


async def run_busy_sync(
    storage: Storage,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> BusySyncResult:
    """Run one busy-sync pass and persist the status.

    Does nothing (returns a zeroed result) when the sync is disabled or no
    write token is connected. All failures are caught, sanitized and stored
    as the last-run error — they never propagate to the caller (the periodic
    calendar sync must not be affected).
    """
    run_at = now or datetime.now(UTC)
    if not settings.is_busy_sync_enabled(storage) or not has_write_token():
        return BusySyncResult(0, 0, 0, 0, storage.count_busy_blocks(), None)

    source_ids = set(settings.get_busy_sync_source_ids(storage))
    window_start, window_end = busy_sync_window(run_at)

    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await run_busy_sync(storage, now=run_at, client=own_client)

    try:
        desired = _desired_events(storage, source_ids, window_start, window_end)
        mapping = {block.source_key: block for block in storage.list_busy_blocks()}
        write_client = BusyWriteClient(busy_write_token_path(), client)
        result = await _reconcile(storage, write_client, desired, mapping, run_at)
    except (
        BusyWriteError,
        httpx.HTTPError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        error = sanitize_error(str(exc))
        logger.warning("Busy sync failed: %s", error)
        settings.set_busy_sync_status(
            storage,
            last_run=run_at.astimezone(UTC).isoformat(),
            active_blocks=storage.count_busy_blocks(),
            error=error,
        )
        return BusySyncResult(0, 0, 0, 0, storage.count_busy_blocks(), error)

    settings.set_busy_sync_status(
        storage,
        last_run=run_at.astimezone(UTC).isoformat(),
        active_blocks=result.active_blocks,
        error=None,
    )
    logger.info(
        "Busy sync: +%d ~%d -%d orphans=%d active=%d",
        result.inserted,
        result.updated,
        result.deleted,
        result.orphans_removed,
        result.active_blocks,
    )
    return result
