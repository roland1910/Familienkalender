"""One-way mirror sync: copy Xalt appointments into a MoreValue calendar.

For every appointment of the configured source calendar(s) within
[today, +180 days) this maintains exactly one real copy — with its actual
title — as a CalDAV resource in the calendar of the configured CalDAV target
source. Roland previously ran an external tool for this direction; the
add-on takes it over, so that tool must be switched off (see CLAUDE.md).

Scope: ALL appointments of the selected calendars, including plain daytime
meetings. The family filter (evening boundary, weekends, holidays) governs
what is DISPLAYED and what goes into the ICS feed — it deliberately has no
say here, because Roland needs his complete work day in MoreValue.

Safety design (see also app.caldav_write):

- Every copy carries the marker properties and lives under the add-on's own
  derived UID/filename; the persisted ``mirror_events`` mapping (source key →
  resource URL + ETag) drives every update and delete, so a write always
  targets a self-created resource inside the configured collection.
- Discovery of existing copies keeps only marker-carrying components, so an
  unmarked (i.e. real) Nextcloud appointment is never a deletion candidate.
- Writes are conditional (If-Match / If-None-Match). A 412 is counted as a
  conflict and left for the next run — a foreign change is never clobbered,
  and the mapping row survives so the deletion is retried.
- Errors are isolated: a mirror-sync failure never blocks the calendar sync.
  The last-run outcome (sanitized error) is persisted for the admin UI.

Feedback protection: the CalDAV read client skips marked components
(app.sources.caldav), so the copies never enter the events table. Without
that they would show up twice in the views/feed AND be mirrored back into
Xalt as "Busy MV" blocks by the busy sync — a loop between the two write
directions.

The sync runs only when it is enabled AND a target calendar is configured.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app import settings, sync_guard
from app.caldav_write import (
    CaldavConflictError,
    CaldavWriteClient,
    CaldavWriteError,
    OwnResource,
    resource_url,
)
from app.models import LOCAL_TZ, AuditEntry, MirrorEvent, Source, StoredEvent
from app.sanitize import sanitize_error
from app.storage import Storage, _encode_moment
from app.sync_identity import desired_source_events
from app.url_validation import SourceURLError

logger = logging.getLogger(__name__)

# How far ahead appointments are mirrored — same horizon as the busy sync.
MIRROR_SYNC_FUTURE_DAYS = 180

# Change-log scope label for the outgoing writes into the MoreValue calendar.
MIRROR_AUDIT_SCOPE = "MoreValue (Spiegel)"

# Reasons the data-loss guard held a run back. Stored as codes (not German
# text) in mirror_sync_status; the admin UI turns them into a sentence —
# same split as slideshow_scan_status/scanWarningText. The rule itself lives
# in app.sync_guard, shared with the birthday sync.
SKIP_SOURCE_ERROR = sync_guard.SKIP_SOURCE_ERROR
SKIP_EMPTY_RESULT = sync_guard.SKIP_EMPTY_RESULT
SKIP_NO_SOURCES = sync_guard.SKIP_NO_SOURCES


def mirror_sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The mirror window: local midnight today to +180 days.

    Past appointments are never written: a copy in the past helps nobody and
    would keep resurrecting entries the user deleted long ago.
    """
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=LOCAL_TZ)
    return start, start + timedelta(days=MIRROR_SYNC_FUTURE_DAYS)


@dataclass(frozen=True)
class MirrorSyncResult:
    """Outcome of one mirror-sync run (for logging/status)."""

    inserted: int
    updated: int
    deleted: int
    orphans_removed: int
    conflicts: int
    active_mirrors: int
    error: str | None
    # Change-log entries for the writes this run performed (outgoing
    # direction). Persisted by run_mirror_sync, isolated from the sync.
    audit_entries: tuple[AuditEntry, ...] = ()
    # The data-loss guard held this run back; nothing was written or deleted.
    skipped: bool = False
    skip_reason: str | None = None


def _empty_result(storage: Storage, error: str | None = None) -> MirrorSyncResult:
    return MirrorSyncResult(0, 0, 0, 0, 0, storage.count_mirror_events(), error)


def _differs(row: MirrorEvent, item: StoredEvent) -> bool:
    """Whether a mapped copy no longer matches its source appointment.

    Compares everything the copy actually carries: title, location, the time
    range and the all-day flag. A pure time SHIFT changes the source key and
    therefore surfaces as delete(old)+insert(new) rather than an update —
    the same identity rule the events table and the busy sync follow.
    """
    event = item.event
    return (
        row.title != event.title
        or (row.location or "") != (event.location or "")
        or row.all_day != event.all_day
        or _encode_moment(row.start) != _encode_moment(event.start)
        or _encode_moment(row.end) != _encode_moment(event.end)
    )


def _mapping_row(
    key: str, url: str, etag: str, item: StoredEvent
) -> MirrorEvent:
    return MirrorEvent(
        source_key=key,
        resource_url=url,
        etag=etag,
        start=item.event.start,
        end=item.event.end,
        all_day=item.event.all_day,
        title=item.event.title,
        location=item.event.location,
    )


def _index_own(
    resources: list[OwnResource],
) -> tuple[dict[str, OwnResource], list[OwnResource]]:
    """Index discovered own copies by source key; extras become orphans.

    A source key should appear at most once. Should the calendar ever hold
    two copies of the same appointment (an interrupted run, a restored
    backup), the first wins and the duplicates are handed back for deletion
    instead of lingering forever.
    """
    by_key: dict[str, OwnResource] = {}
    extras: list[OwnResource] = []
    for resource in resources:
        if resource.source_key and resource.source_key not in by_key:
            by_key[resource.source_key] = resource
        else:
            extras.append(resource)
    return by_key, extras


class _Reconciler:
    """One reconciliation pass; keeps the counters and audit list together."""

    def __init__(
        self,
        storage: Storage,
        client: CaldavWriteClient,
        target_calendar_url: str,
        now: datetime,
    ) -> None:
        self._storage = storage
        self._client = client
        self._calendar_url = target_calendar_url
        self._now = now
        self._ts_iso = now.astimezone(UTC).isoformat()
        self.inserted = self.updated = self.deleted = 0
        self.orphans = self.conflicts = 0
        self.audit: list[AuditEntry] = []

    def _log(self, action: str, title: str, event_start: str | None) -> None:
        self.audit.append(
            AuditEntry(
                ts=self._ts_iso,
                direction="out",
                scope=MIRROR_AUDIT_SCOPE,
                action=action,
                title=title,
                event_start=event_start,
            )
        )

    async def run(
        self,
        desired: dict[str, StoredEvent],
        mapping: dict[str, MirrorEvent],
        resources: list[OwnResource],
    ) -> None:
        own_by_key, extras = _index_own(resources)
        # URLs already dealt with this run, so the orphan pass does not try to
        # delete a resource a second time.
        handled: set[str] = set()

        for key, item in desired.items():
            await self._sync_one(key, item, own_by_key.get(key), mapping.get(key), handled)
        for key, row in mapping.items():
            if key not in desired:
                await self._remove_mapped(key, row, own_by_key.get(key), handled)
        for resource in own_by_key.values():
            if resource.source_key not in desired and resource.url not in handled:
                await self._remove_orphan(resource, handled, drop_mapping=True)
        # Duplicate (or unidentifiable) copies always go, even when their key
        # is still desired — the kept copy is the one indexed above.
        for resource in extras:
            if resource.url not in handled:
                await self._remove_orphan(
                    resource, handled, drop_mapping=resource.source_key not in desired
                )

    async def _sync_one(
        self,
        key: str,
        item: StoredEvent,
        existing: OwnResource | None,
        row: MirrorEvent | None,
        handled: set[str],
    ) -> None:
        if existing is None:
            await self._create(key, item)
            return
        handled.add(existing.url)
        if row is not None and not _differs(row, item):
            # Unchanged. Only refresh the locally remembered validator when
            # the server reports a new one (no request, no change log).
            if row.etag != existing.etag or row.resource_url != existing.url:
                self._storage.upsert_mirror_event(
                    _mapping_row(key, existing.url, existing.etag, item),
                    updated_at=self._now,
                )
            return
        try:
            etag = await self._client.update_event(
                existing.url, key, item.event, etag=existing.etag
            )
        except CaldavConflictError:
            self._note_conflict("update", key)
            return
        self._storage.upsert_mirror_event(
            _mapping_row(key, existing.url, etag, item), updated_at=self._now
        )
        self.updated += 1
        # A missing mapping row means the copy was merely re-adopted (e.g.
        # after a lost mapping); that is housekeeping, not a change Roland
        # needs to see in the change log.
        if row is not None:
            self._log("updated", item.event.title, _encode_moment(item.event.start))

    async def _create(self, key: str, item: StoredEvent) -> None:
        url = resource_url(self._calendar_url, key)
        try:
            etag = await self._client.create_event(url, key, item.event)
        except CaldavConflictError:
            # Something already sits at our derived URL although the listing
            # did not report it (e.g. it lies outside the mirror window).
            # Leave it alone this run; the next one re-reconciles.
            self._note_conflict("create", key)
            return
        self._storage.upsert_mirror_event(
            _mapping_row(key, url, etag, item), updated_at=self._now
        )
        self.inserted += 1
        self._log("added", item.event.title, _encode_moment(item.event.start))

    async def _remove_mapped(
        self,
        key: str,
        row: MirrorEvent,
        existing: OwnResource | None,
        handled: set[str],
    ) -> None:
        """Delete the copy of an appointment that vanished or left the window.

        The URL comes from the mapping (or, when the copy was rediscovered,
        from the listing) — always a resource the add-on created itself.

        A mapped URL can nevertheless have gone STALE: repointing the target
        source's ``calendar_url`` with a plain source PATCH leaves the mapping
        untouched, so its rows suddenly address the old collection. Such a row
        is simply discarded — nothing is touched on the server (we no longer
        know that calendar), and the run carries on. Letting the URL check
        abort the run instead would wedge the mirror at the same row forever.
        """
        url = existing.url if existing is not None else row.resource_url
        etag = existing.etag if existing is not None else row.etag
        if existing is None and not self._client.owns_url(url):
            self._storage.delete_mirror_event(key)
            logger.info(
                "Discarded a mirror mapping row pointing outside the current "
                "calendar (stale target); nothing was deleted on the server."
            )
            return
        # Marked as dealt with up front, so a conflict here does not make the
        # orphan pass attempt the very same resource again in this run.
        handled.add(url)
        try:
            await self._client.delete_event(url, etag=etag)
        except CaldavConflictError:
            # Keep the mapping row so the deletion is retried next run with a
            # fresh ETag from the listing.
            self._note_conflict("delete", key)
            return
        self._storage.delete_mirror_event(key)
        self.deleted += 1
        # The source appointment is gone, so its title comes from the mapping.
        self._log("removed", row.title, _encode_moment(row.start))

    async def _remove_orphan(
        self, resource: OwnResource, handled: set[str], *, drop_mapping: bool
    ) -> None:
        """Delete an own copy that no mapping and no source event backs.

        ``resource`` came out of ``list_own_resources``, i.e. it carries the
        add-on's marker — a foreign appointment can never reach this method.
        ``drop_mapping`` is False for a duplicate copy of a still-wanted
        appointment: its mapping row belongs to the copy that is kept.
        """
        handled.add(resource.url)
        try:
            await self._client.delete_event(resource.url, etag=resource.etag)
        except CaldavConflictError:
            self._note_conflict("delete", resource.source_key)
            return
        if drop_mapping:
            self._storage.delete_mirror_event(resource.source_key)
        self.orphans += 1
        self._log("removed", resource.summary, None)

    def _note_conflict(self, operation: str, key: str) -> None:
        self.conflicts += 1
        # The key contains a foreign UID; log the operation only.
        logger.warning(
            "Mirror sync conflict (%s) — retrying next run (key length %d)",
            operation,
            len(key),
        )

    def result(self) -> MirrorSyncResult:
        return MirrorSyncResult(
            self.inserted,
            self.updated,
            self.deleted,
            self.orphans,
            self.conflicts,
            self._storage.count_mirror_events(),
            None,
            tuple(self.audit),
        )


def _target_source(storage: Storage) -> Source | None:
    """The configured CalDAV target source, or None when unusable.

    Re-validated on read (defence in depth): the target must still exist, be
    a CalDAV source and carry a complete config — the mirror never writes
    anywhere else, and never to a free-form URL.
    """
    target_id = settings.get_mirror_sync_target_source_id(storage)
    if target_id is None:
        return None
    source = storage.get_source(target_id)
    if source is None or source.type != "caldav":
        logger.warning("Mirror target %s is not a usable CalDAV source", target_id)
        return None
    if not all(
        source.config.get(key) for key in ("calendar_url", "username", "app_password")
    ):
        logger.warning("Mirror target %s has an incomplete config", target_id)
        return None
    return source


def _skip_reason(
    source_ids: set[int],
    source_results: dict[int, str | None] | None,
    desired: dict[str, StoredEvent],
    mapping: dict[str, MirrorEvent],
    resources: list[OwnResource] | None,
) -> str | None:
    """Why this run must not reconcile, or None when it may proceed.

    Thin adapter over the shared rule in app.sync_guard (which documents the
    four cases). Called twice per run: once before any network access, and
    once more with the discovered copies — a LOST mapping is protected too,
    because a copy found in the calendar counts just like a mapped one.
    """
    return sync_guard.skip_reason(
        source_ids,
        source_results,
        desired_count=len(desired),
        mapped_count=len(mapping),
        remote_count=len(resources or []),
        label="Mirror sync",
    )


def _skipped_result(storage: Storage, reason: str, run_at: datetime) -> MirrorSyncResult:
    active = storage.count_mirror_events()
    settings.set_mirror_sync_status(
        storage,
        last_run=run_at.astimezone(UTC).isoformat(),
        active_mirrors=active,
        conflicts=0,
        error=None,
        skipped=True,
        skip_reason=reason,
    )
    return MirrorSyncResult(
        0, 0, 0, 0, 0, active, None, (), skipped=True, skip_reason=reason
    )


async def run_mirror_sync(
    storage: Storage,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
    source_results: dict[int, str | None] | None = None,
) -> MirrorSyncResult:
    """Run one mirror-sync pass and persist the status.

    Does nothing (returns a zeroed result) when the sync is disabled or no
    usable CalDAV target is configured. All failures are caught, sanitized and
    stored as the last-run error — they never propagate to the caller (the
    periodic calendar sync must not be affected).

    ``source_results`` is the per-source outcome of the calendar sync that
    just ran (source id -> sanitized error or None). It is what the data-loss
    guard in ``_skip_reason`` needs: without it, an empty result set is never
    taken as permission to delete.
    """
    run_at = now or datetime.now(UTC)
    if not settings.is_mirror_sync_enabled(storage):
        return _empty_result(storage)
    target = _target_source(storage)
    if target is None:
        return _empty_result(storage)

    source_ids = set(settings.get_mirror_sync_source_ids(storage))
    # Mirroring the target calendar into itself would copy the copies.
    source_ids.discard(target.id)
    window_start, window_end = mirror_sync_window(run_at)

    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await run_mirror_sync(
                storage, now=run_at, client=own_client, source_results=source_results
            )

    try:
        desired = desired_source_events(storage, source_ids, window_start, window_end)
        mapping = {row.source_key: row for row in storage.list_mirror_events()}
        # Guards that need no network run before the client is even built.
        reason = _skip_reason(source_ids, source_results, desired, mapping, None)
        if reason is not None:
            return _skipped_result(storage, reason, run_at)
        write_client = CaldavWriteClient(target.config, client)
        resources = await write_client.list_own_resources(window_start, window_end)
        # The listing can reveal copies the (lost) mapping no longer knows —
        # they hold the run back just as mapped ones do.
        reason = _skip_reason(source_ids, source_results, desired, mapping, resources)
        if reason is not None:
            return _skipped_result(storage, reason, run_at)
        reconciler = _Reconciler(
            storage, write_client, target.config["calendar_url"], run_at
        )
        await reconciler.run(desired, mapping, resources)
        result = reconciler.result()
    except (
        CaldavWriteError,
        SourceURLError,
        httpx.HTTPError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        error = sanitize_error(str(exc))
        logger.warning("Mirror sync failed: %s", error)
        settings.set_mirror_sync_status(
            storage,
            last_run=run_at.astimezone(UTC).isoformat(),
            active_mirrors=storage.count_mirror_events(),
            conflicts=0,
            error=error,
        )
        return _empty_result(storage, error)

    # Record what was written into MoreValue — isolated so an audit failure
    # never turns a successful mirror run into an error.
    try:
        storage.add_audit_entries(list(result.audit_entries))
    except Exception:  # pragma: no cover - defensive, audit must never break sync
        logger.exception("Failed to record outgoing change log")
    settings.set_mirror_sync_status(
        storage,
        last_run=run_at.astimezone(UTC).isoformat(),
        active_mirrors=result.active_mirrors,
        conflicts=result.conflicts,
        error=None,
    )
    logger.info(
        "Mirror sync: +%d ~%d -%d orphans=%d conflicts=%d active=%d",
        result.inserted,
        result.updated,
        result.deleted,
        result.orphans_removed,
        result.conflicts,
        result.active_mirrors,
    )
    return result
