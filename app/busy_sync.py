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
  removes blocks whose source event has vanished, again by their own id. The
  same listing carries each block's ``visibility``, so a still-desired block
  written by an older version as "private" is patched back to the current
  "default" once (again via its own mapped id — the invariant is unchanged).
- Errors are isolated: a busy-sync failure never blocks the normal calendar
  sync. The last-run outcome (with a sanitized error) is persisted for the
  admin UI.

Before the diff runs, the wanted appointments are MERGED per occupied time
range (see ``merge_busy_events``) — several calendars holding the same slot
must not produce several identical blocks in Xalt.

The sync runs only when it is enabled AND a write token is connected.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from app import settings
from app.google_busy import (
    BusyWriteClient,
    BusyWriteError,
    busy_write_token_path,
    has_write_token,
)
from app.models import BUSY_BLOCK_TITLE, LOCAL_TZ, AuditEntry, BusyBlock, StoredEvent
from app.sanitize import sanitize_error
from app.storage import Storage, _encode_moment
from app.sync_identity import desired_source_events

logger = logging.getLogger(__name__)

# How far ahead MoreValue appointments are mirrored (Roland's choice).
BUSY_SYNC_FUTURE_DAYS = 180

# Change-log scope label for the outgoing "Busy MV" writes into Xalt.
BUSY_AUDIT_SCOPE = "Xalt (Busy MV)"


def busy_sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The mirror window: local midnight today to +180 days.

    Past appointments are never mirrored — an appointment that already
    started is irrelevant for future free/busy.
    """
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=LOCAL_TZ)
    end = start + timedelta(days=BUSY_SYNC_FUTURE_DAYS)
    return start, end


@dataclass(frozen=True)
class BusySyncResult:
    """Outcome of one busy-sync run (for logging/status)."""

    inserted: int
    updated: int
    deleted: int
    orphans_removed: int
    active_blocks: int
    error: str | None
    # Change-log entries for the writes this run performed (outgoing
    # direction). Persisted by run_busy_sync, isolated from the sync itself.
    audit_entries: tuple[AuditEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class BusySlot:
    """One occupied time range and the appointments that occupy it."""

    # Source key of the winning appointment — the key the mapping row uses.
    key: str
    # The winning appointment; only its times ever reach Google.
    item: StoredEvent
    # Every source key occupying this slot, winner first (rank order). Lets
    # the reconciliation adopt an existing block when the winner changes.
    members: tuple[str, ...]


def _slot_moment(value: datetime | date) -> str:
    """A comparable key for a start/end moment (UTC instant resp. date).

    A tz-naive datetime is read in the family's local zone, never the
    process zone — same rule as the feed's duplicate detection.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return _encode_moment(value)


def _slot_key(item: StoredEvent) -> tuple[str, str, bool]:
    """The identity that decides whether two appointments share a block."""
    event = item.event
    return (
        _slot_moment(event.start),
        _slot_moment(event.end),
        event.all_day,
    )


def merge_busy_events(desired: dict[str, StoredEvent]) -> list[BusySlot]:
    """Collapse the wanted appointments to one block per occupied time range.

    Roland has the same appointment in "Roland MV" AND in "MV Team", and both
    are selected as busy-sync sources — without merging, every source event
    produced its own "Busy MV" block and Xalt showed the slot twice.

    Identity is start + end + all_day ONLY. The title deliberately plays no
    part: the written block is always called "Busy MV", so two DIFFERENT
    parallel appointments still mean nothing more than "busy once" — merging
    them loses no information in the target calendar. Times compare like the
    feed's duplicate detection: timed events as a UTC instant (the same
    moment in another zone is the same slot), all-day events by date, and an
    all-day event never merges with a timed one.

    The winner of a slot is picked DETERMINISTICALLY — lowest ``source_id``,
    then the alphabetically smallest ``uid``, then the smallest source key.
    Anything order-dependent would let the winner flip between runs and turn
    the mapping into an endless delete/insert cycle in a real calendar.

    Returns one ``BusySlot`` per range, ordered by winning key, with every
    member key of the slot attached in the same rank order.
    """
    groups: dict[tuple[str, str, bool], list[tuple[tuple[int, str, str], str, StoredEvent]]]
    groups = {}
    for key, item in desired.items():
        rank = (item.source_id, item.event.uid, key)
        groups.setdefault(_slot_key(item), []).append((rank, key, item))
    slots: list[BusySlot] = []
    for members in groups.values():
        members.sort(key=lambda member: member[0])
        _, winner_key, winner_item = members[0]
        slots.append(
            BusySlot(winner_key, winner_item, tuple(member[1] for member in members))
        )
    slots.sort(key=lambda slot: slot.key)
    return slots


def _block_slot_key(block: BusyBlock) -> tuple[str, str, bool]:
    """The slot a persisted mapping row's block occupies."""
    return (_slot_moment(block.start), _slot_moment(block.end), block.all_day)


def _adopt_existing_blocks(
    storage: Storage,
    slots: list[BusySlot],
    mapping: dict[str, BusyBlock],
    now: datetime,
) -> None:
    """Re-key a mapping row when a slot's winning appointment changed.

    Two situations lead here, and both mean the very same block in Xalt:

    - the winner of a slot vanishes while another appointment still occupies
      the same range (the block should simply follow the new winner), and
    - the merge is rolled out over an existing mapping, where the block that
      survives may be the one of the appointment that lost the merge.

    Without this the diff would delete the old key's block and insert an
    identical one — a pointless change in Roland's real calendar plus two
    entries of noise in the change log. Candidates are matched by the SLOT the
    persisted block occupies (its stored start/end/all_day), which is exactly
    what makes the two blocks interchangeable, and picked in sorted key order
    so the outcome never depends on dictionary order.

    Purely local: only the mapping row is renamed, the Google event id (and
    therefore the block itself) is untouched. The block's private marker keeps
    the old source key as its value; that value is self-description only —
    listing and reconciliation go through the constant owner marker — so
    rewriting it would cost a write into Xalt for no gain.
    """
    wanted = {slot.key for slot in slots}
    # Mapping rows the diff would otherwise delete, grouped by their slot.
    spare: dict[tuple[str, str, bool], list[str]] = {}
    for key, block in mapping.items():
        if key not in wanted:
            spare.setdefault(_block_slot_key(block), []).append(key)
    for keys in spare.values():
        keys.sort()
    for slot in slots:
        if slot.key in mapping:
            continue
        candidates = spare.get(_slot_key(slot.item))
        if not candidates:
            continue
        old_key = candidates.pop(0)
        block = mapping.pop(old_key)
        storage.delete_busy_block(old_key)
        adopted = BusyBlock(
            slot.key, block.google_event_id, block.start, block.end, block.all_day
        )
        storage.upsert_busy_block(adopted, updated_at=now)
        mapping[slot.key] = adopted


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
    # One block per occupied time range, not per source appointment (see
    # merge_busy_events). Blocks of appointments that lost the merge are
    # deleted by phase 2 like any other no-longer-wanted block.
    slots = merge_busy_events(desired)
    _adopt_existing_blocks(storage, slots, mapping, now)
    desired = {slot.key: slot.item for slot in slots}
    # Every Google event id the add-on knows about after this run — a block
    # is legitimate (not an orphan) iff it is still mapped to a desired event.
    # Built as we go so a freshly inserted block is not mistaken for an orphan.
    known_ids: set[str] = set()
    # Ids deleted in phase 2, so the orphan pass (which reuses the start-of-run
    # listing) does not try to delete them a second time.
    deleted_ids: set[str] = set()
    # Outgoing change-log entries for the writes performed this run.
    audit: list[AuditEntry] = []
    ts_iso = now.astimezone(UTC).isoformat()

    # List the add-on's own blocks ONCE (server-side marker filter, never a
    # full scan). Used both to detect visibility drift on mapped blocks and to
    # reconcile orphans. Google omits ``visibility`` when it is "default", so a
    # missing value counts as default (no drift).
    own_blocks = await client.list_own_blocks()
    visibility_by_id: dict[str, str] = {
        own["id"]: (own.get("visibility") or "default")
        for own in own_blocks
        if own.get("id")
    }

    def _audit(action: str, title: str, event_start: str | None) -> None:
        audit.append(
            AuditEntry(
                ts=ts_iso,
                direction="out",
                scope=BUSY_AUDIT_SCOPE,
                action=action,
                title=title,
                event_start=event_start,
            )
        )

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
            _audit("added", item.event.title, _encode_moment(item.event.start))
        else:
            known_ids.add(existing.google_event_id)
            time_changed = _times_differ(existing, item)
            # Correct visibility drift: old blocks were written as "private";
            # patch them back to the current default so they no longer show up
            # flagged private in Xalt. The patch goes through the same event id
            # from the mapping, so the invariant (only own, marked blocks are
            # touched) is unchanged.
            visibility_drift = (
                visibility_by_id.get(existing.google_event_id, "default") != "default"
            )
            if time_changed or visibility_drift:
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
                # A real time change is worth showing in the change log; a
                # pure visibility-drift correction is one-time internal
                # housekeeping (~85 old blocks on the first run) and would
                # flood the log, so it is deliberately not audited.
                if time_changed:
                    _audit("updated", item.event.title, _encode_moment(item.event.start))

    # 2. Delete mapped blocks whose source event is gone or out of window.
    for key, block in mapping.items():
        if key not in desired:
            await client.delete_block(block.google_event_id)
            storage.delete_busy_block(key)
            deleted += 1
            deleted_ids.add(block.google_event_id)
            # The source appointment is gone, so its title is unavailable —
            # the block only ever carried the neutral "Busy MV" title anyway.
            _audit("removed", BUSY_BLOCK_TITLE, _encode_moment(block.start))

    # 3. Reconcile orphans: own blocks in the calendar not backed by a
    #    still-desired mapping (a lost mapping row, a leftover from an earlier
    #    run). Reuses the start-of-run listing, which is restricted to OUR
    #    marker, so no foreign event is ever touched. known_ids holds exactly
    #    the blocks that should remain; deleted_ids skips blocks already
    #    removed in phase 2 (they are still in the start-of-run listing).
    for own in own_blocks:
        event_id = own.get("id")
        if event_id and event_id not in known_ids and event_id not in deleted_ids:
            await client.delete_block(event_id)
            orphans += 1
            _audit("removed", BUSY_BLOCK_TITLE, None)

    active = storage.count_busy_blocks()
    return BusySyncResult(inserted, updated, deleted, orphans, active, None, tuple(audit))


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
        desired = desired_source_events(storage, source_ids, window_start, window_end)
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

    # Record what was written to Xalt in the change log — isolated so an
    # audit-write failure never turns a successful busy sync into an error.
    try:
        storage.add_audit_entries(list(result.audit_entries))
    except Exception:  # pragma: no cover - defensive, audit must never break sync
        logger.exception("Failed to record outgoing change log")
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
