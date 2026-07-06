"""Subscribable ICS feed: Roland's family-relevant appointments.

Content rule: only sources with include_in_feed=True contribute (per-source
admin switch; by default Roland's filtered work calendars), each event
passed through the same family relevance filter as the calendar views
(app.filtering.filter_events with the source's display mode and the
persisted evening boundary). Sources like Marina's or Valentin's calendars
stay out — Marina subscribes to this feed on her phone, so including them
would only duplicate what she already has. The time window matches the
sync window (-7/+90 days).
"""

import hashlib
import re
from datetime import UTC, date, datetime, timedelta

from icalendar import Calendar, Event
from icalendar.prop import vDuration

from app.filtering import filter_events
from app.models import LOCAL_TZ, CalendarEvent, StoredEvent
from app.settings import get_evening_boundary
from app.storage import Storage
from app.sync import sync_window

_WHITESPACE_RUN = re.compile(r"\s+")

PRODID = "-//Familienkalender//github.com/roland1910/Familienkalender//DE"
# The en dash is intentional German typography for the calendar title.
CALENDAR_NAME = "Familie – Roland"  # noqa: RUF001
# Suggested client refresh interval — RFC 7986 REFRESH-INTERVAL plus the
# older X-PUBLISHED-TTL for clients that only understand that one.
REFRESH_INTERVAL = timedelta(minutes=15)


def _stable_uid(item: StoredEvent) -> str:
    """Deterministic per-event UID, stable across feed builds and syncs.

    Built from the storage identity (source_id, uid, start) — the same key
    that deduplicates events in the events table — so subscribing clients
    see updates as updates instead of delete/recreate churn.
    """
    raw = f"{item.source_id}|{item.event.uid}|{item.event.start.isoformat()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{digest}@familienkalender"


def normalize_title(title: str) -> str:
    """Case- and whitespace-insensitive form of an event title.

    Lowercased, outer whitespace trimmed, internal whitespace runs
    collapsed to a single space — so "Team   Sync" and "  team sync "
    compare equal. Used only for the feed's duplicate detection; the
    original title is what actually ends up in the feed.
    """
    return _WHITESPACE_RUN.sub(" ", title).strip().lower()


def _moment_key(value: datetime | date) -> str:
    """A comparable key for a start/end moment.

    Timed events (datetimes) are normalized to a UTC instant so the same
    moment expressed in different zones compares equal; all-day events
    (plain dates) key by their ISO date. The ``all_day`` flag is part of
    the dedup key, so a date and a datetime never collide here.

    A tz-naive datetime is interpreted in the family's local zone
    (Europe/Berlin), never the process timezone, so it lines up with the
    aware Berlin form of the same wall-clock moment.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=LOCAL_TZ)
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def _dedup_key(event: CalendarEvent) -> tuple[str, str, str, bool]:
    """The identity used to decide whether two feed events are duplicates."""
    return (
        normalize_title(event.title),
        _moment_key(event.start),
        _moment_key(event.end),
        event.all_day,
    )


def dedupe_feed_events(items: list[StoredEvent]) -> list[StoredEvent]:
    """Collapse duplicate events *across sources* for the ICS feed.

    Two events are duplicates only when they come from DIFFERENT sources and
    their normalized title, start instant, end instant and all_day flag all
    match (see ``_dedup_key``). Deduplication is strictly cross-source: two
    events from the SAME source (same ``source_id``) are never merged, no
    matter how identical their display key — they are treated as genuinely
    distinct appointments (e.g. two contacts "Oma" and "Müller" whose
    birthdays land on the same day in one contacts source, or two like-named
    meetings at the same time in one calendar). Losing such an event would be
    a real appointment vanishing from the feed.

    Per dedup-key group the *winning source* is the one with the higher
    ``feed_priority`` (ties broken by the lower ``source_id`` for a stable,
    build-independent outcome). ALL events from the winning source in that
    group survive; events from every other source in that group are dropped.
    Consequently cross-source precedence collapses a key to the winning
    source as a whole — the winning source keeps each of its same-key events,
    and lower-priority sources contribute nothing for that key. Priority
    never causes several genuinely distinct events of one source to be
    replaced by a single event of another source.

    Location is deliberately NOT part of the dedup key (Roland's call): the
    same appointment often carries slightly different location text across
    calendars, so only title + start + end + all_day decide identity.

    Non-duplicate events keep their input order. Runs on the raw event titles
    (before the shortcode prefix is added in ``build_feed``) and only affects
    the feed — the calendar views keep showing every source's own chip.
    """
    # First pass: find the winning source (feed_priority, then lower id) per
    # dedup key. -source_id makes the lower id win a priority tie under max().
    winning_source: dict[tuple[str, str, str, bool], tuple[int, int]] = {}
    for item in items:
        key = _dedup_key(item.event)
        rank = (item.feed_priority, -item.source_id)
        best = winning_source.get(key)
        if best is None or rank > best:
            winning_source[key] = rank
    # Second pass: keep every event whose own source is the winning source for
    # its key. Same-source events are thus all retained; other sources drop.
    return [
        item
        for item in items
        if (item.feed_priority, -item.source_id)
        == winning_source[_dedup_key(item.event)]
    ]


def build_feed(storage: Storage, *, now: datetime | None = None) -> bytes:
    """The complete ICS document (VCALENDAR bytes) for the feed."""
    window_start, window_end = sync_window(now)
    boundary = get_evening_boundary(storage)
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", CALENDAR_NAME)
    calendar.add(
        "refresh-interval",
        vDuration(REFRESH_INTERVAL),
        parameters={"VALUE": "DURATION"},
    )
    calendar.add("x-published-ttl", vDuration(REFRESH_INTERVAL))
    # One DTSTAMP for the whole build; required per VEVENT by RFC 5545.
    dtstamp = (now or datetime.now(UTC)).astimezone(UTC)
    # Collect the feed-eligible, family-filtered events first, then collapse
    # cross-source duplicates before emitting VEVENTs (see dedupe_feed_events).
    eligible = [
        item
        for item in storage.get_events(window_start, window_end)
        if item.include_in_feed
        and filter_events(
            [item.event], display_mode=item.display_mode, boundary=boundary
        )
    ]
    for item in dedupe_feed_events(eligible):
        event = item.event
        component = Event()
        component.add("uid", _stable_uid(item))
        component.add("dtstamp", dtstamp)
        title = f"{item.shortcode} {event.title}" if item.shortcode else event.title
        component.add("summary", title)
        if event.all_day:
            # Plain dates keep the iCalendar all-day semantics (VALUE=DATE,
            # exclusive end date) exactly as stored.
            component.add("dtstart", event.start)
            component.add("dtend", event.end)
        else:
            component.add("dtstart", event.start_as_datetime().astimezone(UTC))
            component.add("dtend", event.end_as_datetime().astimezone(UTC))
        if event.location:
            component.add("location", event.location)
        calendar.add_component(component)
    return calendar.to_ical()
