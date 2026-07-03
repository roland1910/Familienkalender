"""Shared domain models for calendar sources and events."""

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

# The family lives in Germany; all display and filtering decisions are made
# in this timezone. Will become configurable with the admin UI if ever needed.
LOCAL_TZ = ZoneInfo("Europe/Berlin")

SOURCE_TYPES = ("google", "caldav")
DISPLAY_MODES = ("full", "filtered")


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A single (already recurrence-expanded) calendar event occurrence.

    Timed events carry timezone-aware datetimes; all-day events carry plain
    dates with an exclusive end date (iCalendar semantics: a one-day all-day
    event on 2026-07-12 has end 2026-07-13).
    """

    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    location: str | None = None

    def start_as_datetime(self) -> datetime:
        """Start as an aware datetime (all-day: local midnight)."""
        return as_local_datetime(self.start)

    def end_as_datetime(self) -> datetime:
        """Exclusive end as an aware datetime (all-day: local midnight)."""
        return as_local_datetime(self.end)


def as_local_datetime(value: datetime | date) -> datetime:
    """Normalize a start/end value to an aware datetime.

    Plain dates (all-day events) become local midnight, because all-day
    events are bound to calendar days in the family's timezone.
    """
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.min, tzinfo=LOCAL_TZ)


@dataclass(frozen=True, slots=True)
class Source:
    """A configured calendar source."""

    id: int
    type: str
    name: str
    config: dict[str, Any]
    enabled: bool
    display_mode: str
    last_sync_at: datetime | None
    last_sync_error: str | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event as read back from storage, with source metadata attached."""

    source_id: int
    source_name: str
    display_mode: str
    event: CalendarEvent
