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
from datetime import UTC, datetime, timedelta

from icalendar import Calendar, Event
from icalendar.prop import vDuration

from app.filtering import filter_events
from app.models import StoredEvent
from app.settings import get_evening_boundary
from app.storage import Storage
from app.sync import sync_window

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
    for item in storage.get_events(window_start, window_end):
        if not item.include_in_feed:
            continue
        if not filter_events(
            [item.event], display_mode=item.display_mode, boundary=boundary
        ):
            continue
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
