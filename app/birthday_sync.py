"""Birthday sync: contact birthdays as yearly series in Roland's calendars.

Roland wants the birthdays from his contact sources (source type
``google_contacts``) in the calendars he uses at work: his Google (Xalt)
calendar and/or his Nextcloud calendar. Both targets are optional and
switched independently.

**One yearly series per person, not one copy per year.** The contact source
produces one all-day event per person *and year that falls into the sync
window* (see app.sources.google_contacts). Writing those out one by one would
mean re-writing every birthday every year and would fill the target calendars
with near-duplicates. Instead this sync maintains exactly ONE all-day entry
per person with ``RRULE:FREQ=YEARLY`` — birthdays are recurring by nature, and
the series keeps working even if the add-on never writes again.

**Person key.** The source event's UID is ``<contact resource>|<year>``; the
year is stripped off to get a stable, year-INDEPENDENT identity
``<source-id>|<contact resource>`` (see ``person_key``). Prefixing the source
id keeps two contact sources apart — the same person in Roland's and in
Marina's contacts is deliberately two series, exactly as the calendar view
already shows two chips.

**29 February.** A leap-day birthday is written with 28 February as the
series start (``series_start``). A yearly RRULE starting on 29 February only
fires in leap years, i.e. Roland would see the birthday every four years; the
source already falls back to the 28th in non-leap years, so the 28th is both
the consistent and the useful choice.

**Marker separation — the point to get right.** Three syncs write into these
two calendars (busy sync, mirror sync, birthday sync). Each carries its own
markers so it only ever sees, updates and deletes its OWN entries:

- Google: ``extendedProperties.private.familienkalender_owner`` carries the
  PURPOSE ("1" busy, "birthday"), and Google's ``privateExtendedProperty``
  filter matches exactly — so a listing returns one purpose only. The
  purpose-specific key (``familienkalender_birthday``) is checked a second
  time client-side.
- CalDAV: the birthday series live in their own UID namespace
  (``familienkalender-birthday-…``, see app.caldav_write.BIRTHDAY_NAMESPACE)
  and carry ``X-FAMILIENKALENDER-BIRTHDAY``; the mirror sync demands its own
  prefix, so neither can report the other's objects as an orphan.

**Deleting only on knowledge, never on absence.** The events table only holds
the sync window (-7/+90 days), so most people simply have no source event
right now — their absence says nothing. A mapped series is therefore only
deleted when its own date falls INSIDE the current window and the person is
still missing from the desired set; outside the window it is left untouched.
On top of that the shared data-loss guard (app.sync_guard) can hold a whole
run back.

Feedback protection: both read clients skip what the add-on wrote itself (the
private marker in app.sources.google, the owner X-property in
app.sources.caldav), so a series never returns as a calendar event — which
would otherwise duplicate the contact source AND get copied on by the mirror
sync.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

from app import settings, sync_guard
from app.caldav_write import (
    BIRTHDAY_NAMESPACE,
    CaldavConflictError,
    CaldavWriteClient,
    CaldavWriteError,
    resource_url,
)
from app.google_busy import (
    BIRTHDAY_MARKER_KEY,
    BusyWriteClient,
    BusyWriteError,
    birthday_event_body,
    busy_write_token_path,
    has_write_token,
)
from app.models import (
    BIRTHDAY_TARGETS,
    LOCAL_TZ,
    AuditEntry,
    BirthdayBlock,
    CalendarEvent,
    Source,
)
from app.sanitize import sanitize_error
from app.sources.google_contacts import occurrence_in_year
from app.storage import Storage
from app.url_validation import SourceURLError

logger = logging.getLogger(__name__)

TARGET_GOOGLE, TARGET_CALDAV = BIRTHDAY_TARGETS

# The window in which the events table actually holds birthday occurrences.
# Mirrors SYNC_WINDOW_* in app.sync (imported there, not here, to keep the
# dependency one-way: app.sync drives this module).
BIRTHDAY_WINDOW_PAST_DAYS = 7
BIRTHDAY_WINDOW_FUTURE_DAYS = 90

# A recurring VEVENT only matches a CalDAV time-range query that one of its
# OCCURRENCES falls into, so the listing window must span more than a year —
# otherwise every series outside the narrow window would look missing and the
# next create would collide with the existing resource.
CALDAV_LIST_FUTURE_DAYS = 400

# Change-log scopes of the two write targets.
GOOGLE_AUDIT_SCOPE = "Geburtstage (Xalt)"
CALDAV_AUDIT_SCOPE = "Geburtstage (MoreValue)"

# The source event UID is "<contact resource>|<year>"; only that exact
# four-digit year suffix is stripped, so a contact resource that happens to
# contain a pipe keeps its identity.
_YEAR_SUFFIX = re.compile(r"\|\d{4}$")


def person_key(source_id: int, uid: str) -> str:
    """Stable, year-independent identity of one contact's birthday."""
    return f"{source_id}|{_YEAR_SUFFIX.sub('', uid)}"


def series_start(day: date) -> date:
    """The DTSTART of the yearly series for a birthday on ``day``.

    29 February becomes 28 February: a yearly recurrence starting on the leap
    day only fires every four years, while the source itself already shows the
    birthday on the 28th in non-leap years.
    """
    if day.month == 2 and day.day == 29:
        return day.replace(day=28)
    return day


def occurs_in_window(day: date, window_start: date, window_end: date) -> bool:
    """Whether a birthday on ``day``'s month/day falls into [start, end).

    This is what makes "the person is gone" decidable: inside the window the
    desired set is complete, so a missing person really is missing. Outside
    it, the source simply produced no event and nothing may be concluded.
    """
    for year in range(window_start.year, window_end.year + 1):
        occurrence = occurrence_in_year(day.month, day.day, year)
        if occurrence is not None and window_start <= occurrence < window_end:
            return True
    return False


def birthday_sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The window the events table covers: local midnight -7 to +90 days."""
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start_day = now_local.date() - timedelta(days=BIRTHDAY_WINDOW_PAST_DAYS)
    end_day = now_local.date() + timedelta(days=BIRTHDAY_WINDOW_FUTURE_DAYS)
    return (
        datetime.combine(start_day, datetime.min.time(), tzinfo=LOCAL_TZ),
        datetime.combine(end_day, datetime.min.time(), tzinfo=LOCAL_TZ),
    )


@dataclass(frozen=True, slots=True)
class DesiredBirthday:
    """One person's birthday as it should exist in the target calendars."""

    person_key: str
    title: str
    start: date


@dataclass(frozen=True, slots=True)
class RemoteBirthday:
    """One birthday series discovered in a target calendar.

    Only ever built from a marker-filtered listing, so it is by construction
    one of the birthday sync's own entries — never a foreign appointment and
    never another sync's object.
    """

    person_key: str
    remote_id: str
    title: str
    start: date | None
    etag: str = ""


@dataclass
class BirthdaySyncResult:
    """Outcome of one birthday-sync run (for logging/status)."""

    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    orphans_removed: int = 0
    conflicts: int = 0
    active_google: int = 0
    active_caldav: int = 0
    error: str | None = None
    audit_entries: tuple[AuditEntry, ...] = ()
    skipped: bool = False
    skip_reason: str | None = None


def desired_birthdays(
    storage: Storage,
    source_ids: set[int],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, DesiredBirthday]:
    """One entry per person from the selected contact sources in the window.

    The source emits one all-day event per person and year; they collapse
    here into a single series per person (earliest occurrence wins — with a
    97-day window there is at most one anyway, but "earliest" keeps the result
    deterministic).
    """
    desired: dict[str, DesiredBirthday] = {}
    for item in storage.get_events(window_start, window_end):
        if item.source_id not in source_ids or not item.event.all_day:
            continue
        start = item.event.start
        if isinstance(start, datetime):  # pragma: no cover - all-day is a date
            start = start.date()
        if not (window_start.date() <= start < window_end.date()):
            continue
        key = person_key(item.source_id, item.event.uid)
        candidate = DesiredBirthday(key, item.event.title, series_start(start))
        previous = desired.get(key)
        if previous is None or candidate.start < previous.start:
            desired[key] = candidate
    return desired


def _differs(row: BirthdayBlock, want: DesiredBirthday) -> bool:
    """Whether a mapped series no longer matches the person's birthday.

    The YEAR of the stored start is deliberately ignored: a yearly series is
    timeless, and the source hands out a new year's occurrence every year —
    comparing it would rewrite every series once a year for nothing.
    """
    return (
        row.title != want.title
        or row.start.month != want.start.month
        or row.start.day != want.start.day
    )


class _GoogleTarget:
    """Write side for the Google (Xalt) primary calendar."""

    name = TARGET_GOOGLE
    audit_scope = GOOGLE_AUDIT_SCOPE

    def __init__(self, client: BusyWriteClient) -> None:
        self._client = client

    async def list_existing(self) -> list[RemoteBirthday]:
        found = []
        for item in await self._client.list_own_birthdays():
            private = (item.get("extendedProperties") or {}).get("private") or {}
            found.append(
                RemoteBirthday(
                    person_key=str(private.get(BIRTHDAY_MARKER_KEY) or ""),
                    remote_id=str(item["id"]),
                    title=str(item.get("summary") or ""),
                    start=_google_start(item),
                )
            )
        return found

    async def create(self, want: DesiredBirthday) -> tuple[str, str]:
        event_id = await self._client.insert_event(
            birthday_event_body(want.person_key, want.title, want.start)
        )
        return event_id, ""

    async def update(
        self, remote: RemoteBirthday, want: DesiredBirthday
    ) -> tuple[str, str]:
        await self._client.patch_event(
            remote.remote_id,
            birthday_event_body(want.person_key, want.title, want.start),
        )
        return remote.remote_id, ""

    async def delete(self, remote_id: str, etag: str) -> None:  # noqa: ARG002 - no ETag on Google
        await self._client.delete_event(remote_id)

    def owns(self, remote_id: str) -> bool:
        """Google event ids always come from our own mapping/listing."""
        return bool(remote_id)


def _google_start(item: dict) -> date | None:
    raw = (item.get("start") or {}).get("date")
    try:
        return date.fromisoformat(raw) if raw else None
    except (ValueError, TypeError):
        return None


class _CaldavTarget:
    """Write side for a configured CalDAV (Nextcloud) calendar."""

    name = TARGET_CALDAV
    audit_scope = CALDAV_AUDIT_SCOPE

    def __init__(
        self, client: CaldavWriteClient, collection: str, now: datetime
    ) -> None:
        self._client = client
        self._collection = collection
        self._now = now

    async def list_existing(self) -> list[RemoteBirthday]:
        start, end = _caldav_list_window(self._now)
        found = []
        for resource in await self._client.list_own_resources(start, end):
            value = resource.start
            if isinstance(value, datetime):
                value = value.date()
            found.append(
                RemoteBirthday(
                    person_key=resource.source_key,
                    remote_id=resource.url,
                    title=resource.summary,
                    start=value if isinstance(value, date) else None,
                    etag=resource.etag,
                )
            )
        return found

    async def create(self, want: DesiredBirthday) -> tuple[str, str]:
        url = resource_url(
            self._collection, want.person_key, namespace=BIRTHDAY_NAMESPACE
        )
        etag = await self._client.create_event(
            url, want.person_key, _as_event(want), yearly=True
        )
        return url, etag

    async def update(
        self, remote: RemoteBirthday, want: DesiredBirthday
    ) -> tuple[str, str]:
        etag = await self._client.update_event(
            remote.remote_id,
            want.person_key,
            _as_event(want),
            etag=remote.etag,
            yearly=True,
        )
        return remote.remote_id, etag

    async def delete(self, remote_id: str, etag: str) -> None:
        await self._client.delete_event(remote_id, etag=etag)

    def owns(self, remote_id: str) -> bool:
        return self._client.owns_url(remote_id)


def _caldav_list_window(now: datetime) -> tuple[datetime, datetime]:
    """Wide enough that EVERY yearly series has an occurrence inside it."""
    return now - timedelta(days=BIRTHDAY_WINDOW_PAST_DAYS), now + timedelta(
        days=CALDAV_LIST_FUTURE_DAYS
    )


def _as_event(want: DesiredBirthday) -> CalendarEvent:
    """The all-day CalendarEvent the CalDAV writer serializes.

    Exclusive end date (iCalendar semantics); no location, no description —
    the title is all a birthday carries.
    """
    return CalendarEvent(
        uid=want.person_key,
        title=want.title,
        start=want.start,
        end=want.start + timedelta(days=1),
        all_day=True,
    )


@dataclass
class _Counters:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    orphans: int = 0
    conflicts: int = 0
    audit: list[AuditEntry] = field(default_factory=list)


class _Reconciler:
    """One reconciliation pass against ONE target calendar."""

    def __init__(
        self,
        storage: Storage,
        target: _GoogleTarget | _CaldavTarget,
        now: datetime,
        window: tuple[date, date],
        counters: _Counters,
    ) -> None:
        self._storage = storage
        self._target = target
        self._now = now
        self._window = window
        self._counters = counters
        self._ts_iso = now.astimezone(UTC).isoformat()

    def _log(self, action: str, title: str, when: date | None) -> None:
        self._counters.audit.append(
            AuditEntry(
                ts=self._ts_iso,
                direction="out",
                scope=self._target.audit_scope,
                action=action,
                title=title,
                event_start=when.isoformat() if when else None,
            )
        )

    def _remember(
        self, key: str, remote_id: str, etag: str, start: date, title: str
    ) -> None:
        self._storage.upsert_birthday_block(
            BirthdayBlock(
                person_key=key,
                target=self._target.name,
                remote_id=remote_id,
                start=start,
                title=title,
                etag=etag,
            ),
            updated_at=self._now,
        )

    async def run(
        self,
        desired: dict[str, DesiredBirthday],
        mapping: dict[str, BirthdayBlock],
        remote: list[RemoteBirthday],
    ) -> None:
        by_key: dict[str, RemoteBirthday] = {}
        extras: list[RemoteBirthday] = []
        for entry in remote:
            if entry.person_key and entry.person_key not in by_key:
                by_key[entry.person_key] = entry
            else:
                extras.append(entry)

        for key, want in desired.items():
            await self._sync_one(key, want, by_key.get(key), mapping.get(key))
        for key, row in mapping.items():
            if key not in desired:
                await self._remove_mapped(key, row, by_key.get(key))
        for entry in by_key.values():
            if entry.person_key not in desired and entry.person_key not in mapping:
                await self._handle_unmapped(entry)
        # A second entry for the same person is never wanted: the one kept is
        # the one indexed above, so this duplicate goes regardless of window.
        for entry in extras:
            await self._delete(entry.remote_id, entry.etag, entry.title, entry.start)

    async def _sync_one(
        self,
        key: str,
        want: DesiredBirthday,
        remote: RemoteBirthday | None,
        row: BirthdayBlock | None,
    ) -> None:
        if remote is None:
            try:
                remote_id, etag = await self._target.create(want)
            except CaldavConflictError:
                self._note_conflict("create")
                return
            self._remember(key, remote_id, etag, want.start, want.title)
            self._counters.inserted += 1
            self._log("added", want.title, want.start)
            return
        if row is not None and not _differs(row, want):
            # Unchanged. Only refresh what the server reported (no request,
            # no change-log entry) — re-adoption is housekeeping, not news.
            if row.remote_id != remote.remote_id or row.etag != remote.etag:
                self._remember(key, remote.remote_id, remote.etag, row.start, row.title)
            return
        try:
            remote_id, etag = await self._target.update(remote, want)
        except CaldavConflictError:
            self._note_conflict("update")
            return
        self._remember(key, remote_id, etag, want.start, want.title)
        self._counters.updated += 1
        if row is not None:
            self._log("updated", want.title, want.start)

    async def _remove_mapped(
        self, key: str, row: BirthdayBlock, remote: RemoteBirthday | None
    ) -> None:
        """Delete a series whose person is gone — but only if we can tell.

        Outside the current window the source produces no event for that
        person, so their absence from the desired set means nothing and the
        series is left alone.
        """
        if not occurs_in_window(row.start, *self._window):
            return
        remote_id = remote.remote_id if remote is not None else row.remote_id
        etag = remote.etag if remote is not None else row.etag
        if remote is None and not self._target.owns(remote_id):
            # A stale address (the target was repointed without clearing the
            # mapping): drop the row, touch nothing on the server, carry on.
            self._storage.delete_birthday_block(key, self._target.name)
            logger.info(
                "Discarded a birthday mapping row addressing another calendar."
            )
            return
        if await self._delete(remote_id, etag, row.title, row.start, orphan=False):
            self._storage.delete_birthday_block(key, self._target.name)

    async def _handle_unmapped(self, entry: RemoteBirthday) -> None:
        """An own series with no mapping row and no desired person.

        In-window means the person really is gone → delete. Out of window we
        know nothing, so the entry is adopted into the mapping instead: a
        later run, when its date IS in the window, can then judge it.
        """
        if entry.start is not None and not occurs_in_window(entry.start, *self._window):
            self._remember(
                entry.person_key,
                entry.remote_id,
                entry.etag,
                entry.start,
                entry.title,
            )
            return
        await self._delete(entry.remote_id, entry.etag, entry.title, entry.start)

    async def _delete(
        self,
        remote_id: str,
        etag: str,
        title: str,
        when: date | None,
        *,
        orphan: bool = True,
    ) -> bool:
        try:
            await self._target.delete(remote_id, etag)
        except CaldavConflictError:
            # Leave the mapping row so the deletion is retried next run with
            # a fresh ETag from the listing.
            self._note_conflict("delete")
            return False
        if orphan:
            self._counters.orphans += 1
        else:
            self._counters.deleted += 1
        self._log("removed", title, when)
        return True

    def _note_conflict(self, operation: str) -> None:
        self._counters.conflicts += 1
        logger.warning("Birthday sync conflict (%s) — retrying next run", operation)


def _caldav_target_source(storage: Storage) -> Source | None:
    """The configured CalDAV target source, or None when unusable.

    Re-validated on read (defence in depth): the target must still exist, be
    a CalDAV source and carry a complete config — the birthday sync never
    writes anywhere else, and never to a free-form URL.
    """
    target_id = settings.get_birthday_sync_caldav_target_id(storage)
    if target_id is None:
        return None
    source = storage.get_source(target_id)
    if source is None or source.type != "caldav":
        logger.warning("Birthday target %s is not a usable CalDAV source", target_id)
        return None
    if not all(
        source.config.get(key) for key in ("calendar_url", "username", "app_password")
    ):
        logger.warning("Birthday target %s has an incomplete config", target_id)
        return None
    return source


def _selected_source_ids(storage: Storage) -> set[int]:
    """The selected sources, restricted to existing contact sources.

    Defence in depth against a stale/edited setting: only ``google_contacts``
    sources can meaningfully feed this sync, and turning every appointment of
    a normal calendar into a YEARLY series would be a mess.
    """
    known = {
        source.id
        for source in storage.list_sources()
        if source.type == "google_contacts"
    }
    return {sid for sid in settings.get_birthday_sync_source_ids(storage) if sid in known}


def _status(storage: Storage) -> tuple[int, int]:
    return (
        storage.count_birthday_blocks(TARGET_GOOGLE),
        storage.count_birthday_blocks(TARGET_CALDAV),
    )


def _idle_result(storage: Storage, error: str | None = None) -> BirthdaySyncResult:
    google, caldav = _status(storage)
    return BirthdaySyncResult(
        active_google=google, active_caldav=caldav, error=error
    )


def _persist_status(
    storage: Storage, run_at: datetime, result: BirthdaySyncResult
) -> None:
    settings.set_birthday_sync_status(
        storage,
        last_run=run_at.astimezone(UTC).isoformat(),
        active_google=result.active_google,
        active_caldav=result.active_caldav,
        conflicts=result.conflicts,
        error=result.error,
        skipped=result.skipped,
        skip_reason=result.skip_reason,
    )


async def _build_targets(
    storage: Storage, client: httpx.AsyncClient, now: datetime
) -> list[_GoogleTarget | _CaldavTarget]:
    targets: list[_GoogleTarget | _CaldavTarget] = []
    if settings.is_birthday_sync_google_enabled(storage) and has_write_token():
        targets.append(_GoogleTarget(BusyWriteClient(busy_write_token_path(), client)))
    caldav_source = _caldav_target_source(storage)
    if caldav_source is not None:
        collection = caldav_source.config["calendar_url"]
        targets.append(
            _CaldavTarget(
                CaldavWriteClient(
                    caldav_source.config, client, namespace=BIRTHDAY_NAMESPACE
                ),
                collection,
                now,
            )
        )
    return targets


async def run_birthday_sync(
    storage: Storage,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
    source_results: dict[int, str | None] | None = None,
) -> BirthdaySyncResult:
    """Run one birthday-sync pass and persist the status.

    Does nothing (returns a zeroed result) when the sync is disabled or no
    target is usable. All failures are caught, sanitized and stored as the
    last-run error — they never propagate to the caller (the periodic calendar
    sync must not be affected).

    ``source_results`` is the per-source outcome of the calendar sync that
    just ran; the shared data-loss guard needs it before it may delete
    anything (see app.sync_guard).
    """
    run_at = now or datetime.now(UTC)
    if not settings.is_birthday_sync_enabled(storage):
        return _idle_result(storage)

    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await run_birthday_sync(
                storage, now=run_at, client=own_client, source_results=source_results
            )

    source_ids = _selected_source_ids(storage)
    window_start, window_end = birthday_sync_window(run_at)
    date_window = (window_start.date(), window_end.date())

    try:
        targets = await _build_targets(storage, client, run_at)
        if not targets:
            return _idle_result(storage)
        desired = desired_birthdays(storage, source_ids, window_start, window_end)
        mapping = storage.list_birthday_blocks()
        listings: dict[str, list[RemoteBirthday]] = {}
        reason = sync_guard.skip_reason(
            source_ids,
            source_results,
            desired_count=len(desired),
            mapped_count=len(mapping),
            label="Birthday sync",
        )
        if reason is None:
            listings = {target.name: await target.list_existing() for target in targets}
            reason = sync_guard.skip_reason(
                source_ids,
                source_results,
                desired_count=len(desired),
                mapped_count=len(mapping),
                remote_count=sum(len(entries) for entries in listings.values()),
                label="Birthday sync",
            )
        if reason is not None:
            google, caldav = _status(storage)
            result = BirthdaySyncResult(
                active_google=google,
                active_caldav=caldav,
                skipped=True,
                skip_reason=reason,
            )
            _persist_status(storage, run_at, result)
            return result

        counters = _Counters()
        for target in targets:
            rows = {
                row.person_key: row
                for row in mapping
                if row.target == target.name
            }
            await _Reconciler(
                storage, target, run_at, date_window, counters
            ).run(desired, rows, listings[target.name])
    except (
        BusyWriteError,
        CaldavWriteError,
        SourceURLError,
        httpx.HTTPError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        error = sanitize_error(str(exc))
        logger.warning("Birthday sync failed: %s", error)
        result = _idle_result(storage, error)
        _persist_status(storage, run_at, result)
        return result

    google, caldav = _status(storage)
    result = BirthdaySyncResult(
        inserted=counters.inserted,
        updated=counters.updated,
        deleted=counters.deleted,
        orphans_removed=counters.orphans,
        conflicts=counters.conflicts,
        active_google=google,
        active_caldav=caldav,
        audit_entries=tuple(counters.audit),
    )
    # Record what was written — isolated so an audit failure never turns a
    # successful run into an error.
    try:
        storage.add_audit_entries(list(result.audit_entries))
    except Exception:  # pragma: no cover - defensive, audit must never break sync
        logger.exception("Failed to record outgoing change log")
    _persist_status(storage, run_at, result)
    logger.info(
        "Birthday sync: +%d ~%d -%d orphans=%d conflicts=%d active=%d/%d",
        result.inserted,
        result.updated,
        result.deleted,
        result.orphans_removed,
        result.conflicts,
        result.active_google,
        result.active_caldav,
    )
    return result
