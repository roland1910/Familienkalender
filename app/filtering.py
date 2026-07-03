"""Family relevance filter for calendar sources with display_mode=filtered.

Work calendars (display_mode=filtered) only contribute events that matter
to the family's evening and weekend planning:

- events that reach past the evening boundary (default 17:00) or lie
  entirely after it,
- events spanning more than one local calendar day (overnight stays,
  business trips), and
- all-day events.

Plain intra-day meetings are dropped. All decisions are made in the local
timezone (Europe/Berlin): source calendars may deliver events in UTC or
any other timezone.
"""

from collections.abc import Iterable
from datetime import datetime, time, timedelta

from app.models import LOCAL_TZ, CalendarEvent

DEFAULT_EVENING_BOUNDARY = time(17, 0)


def is_family_relevant(
    event: CalendarEvent, *, boundary: time = DEFAULT_EVENING_BOUNDARY
) -> bool:
    """Decide whether a single event is shown for a filtered source."""
    if event.all_day:
        # All-day events are always shown, even single-day ones: they carry
        # family-relevant info (public holidays, birthdays, trips) and are
        # rare in work calendars, so the noise risk is low.
        return True

    start_local = event.start_as_datetime().astimezone(LOCAL_TZ)
    end_local = event.end_as_datetime().astimezone(LOCAL_TZ)

    # More than one local calendar day (overnight or longer) is always
    # relevant. An event ending exactly at midnight is treated as ending on
    # the previous day (exclusive end), not as a multi-day event.
    end_day = (end_local - timedelta(microseconds=1)).date()
    if end_day > start_local.date():
        return True

    # Same local day: relevant only if it reaches past the evening boundary.
    # Compare full datetimes, not times: an event ending at midnight sharp
    # has end.time() == 00:00 but clearly reaches into the evening. An event
    # ending exactly at the boundary does not reach into the evening and is
    # dropped; anything ending later (which includes every event starting at
    # or after the boundary) is shown.
    boundary_moment = datetime.combine(start_local.date(), boundary, tzinfo=LOCAL_TZ)
    return end_local > boundary_moment


def filter_events(
    events: Iterable[CalendarEvent],
    *,
    display_mode: str,
    boundary: time = DEFAULT_EVENING_BOUNDARY,
) -> list[CalendarEvent]:
    """Apply the relevance filter according to the source's display mode."""
    if display_mode == "full":
        return list(events)
    return [event for event in events if is_family_relevant(event, boundary=boundary)]
