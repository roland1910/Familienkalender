"""Shared domain models for calendar sources and events."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

# The family lives in Germany; all display and filtering decisions are made
# in this timezone. Will become configurable with the admin UI if ever needed.
LOCAL_TZ = ZoneInfo("Europe/Berlin")

SOURCE_TYPES = ("google", "caldav")
DISPLAY_MODES = ("full", "filtered")

# Optional per-source shortcode, used as a title prefix in the subscribable
# ICS feed (e.g. "RX Kundentermin"). Empty means "no prefix".
SHORTCODE_MAX_LENGTH = 6
SHORTCODE_PATTERN = re.compile(rf"^[A-Z0-9]{{0,{SHORTCODE_MAX_LENGTH}}}$")


def is_valid_shortcode(value: str) -> bool:
    """Whether ``value`` is an acceptable source shortcode (may be empty)."""
    return SHORTCODE_PATTERN.fullmatch(value) is not None


# Optional per-source display color. Strictly "#rrggbb" (lowercase hex) or
# empty ("use the frontend's default palette"). The value ends up
# interpolated into a CSS custom property in the frontend, so nothing
# beyond this exact shape may ever be stored (CSS injection guard); the
# frontend re-validates defensively before using it.
SOURCE_COLOR_PATTERN = re.compile(r"^(#[0-9a-f]{6})?$")


def is_valid_source_color(value: str) -> bool:
    """Whether ``value`` is an acceptable source color (may be empty)."""
    return SOURCE_COLOR_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class TagOption:
    """One selectable day-tag symbol (id is stable, emoji is the display)."""

    id: str
    emoji: str


# Fixed catalog of day-tag symbols. Single source of truth for the backend
# whitelist (storage validation) and the frontend picker (served via
# GET /api/tags/options). Planned to become admin-configurable later.
TAG_OPTIONS = (
    TagOption("happy", "😀"),
    TagOption("heart", "❤️"),
    TagOption("star", "⭐"),
    TagOption("party", "🎉"),
    TagOption("soccer", "⚽"),
    TagOption("birthday", "🎂"),
    TagOption("travel", "✈️"),
    TagOption("sun", "🌞"),
    TagOption("sad", "🙁"),
)

# Cap per day: the tags must fit next to the day number in a month cell,
# and more than a handful of symbols per day carries no meaning anyway.
MAX_TAGS_PER_DAY = 3


class UnknownTagError(ValueError):
    """Raised when a day-tag write contains an emoji outside TAG_OPTIONS."""


class TagLimitError(ValueError):
    """Raised when a day-tag write would exceed MAX_TAGS_PER_DAY."""


class TagDateOutOfRangeError(ValueError):
    """Raised when a day-tag write targets a date outside the allowed window."""


# Day-tags are a small, purely local feature; there is no legitimate reason
# to tag a day far outside this window. Bounding it keeps the day_tags table
# from growing unbounded via scripted/malicious requests with wild dates.
TAG_DATE_PAST_YEARS = 2
TAG_DATE_FUTURE_YEARS = 10


def _shift_years(reference: date, years: int) -> date:
    """reference + years, clamped to a valid day (handles 29 Feb safely)."""
    try:
        return reference.replace(year=reference.year + years)
    except ValueError:
        # 29 Feb shifted onto a non-leap year: fall back to 28 Feb.
        return reference.replace(month=2, day=28, year=reference.year + years)


def is_tag_date_in_range(day: date, *, today: date | None = None) -> bool:
    """Whether ``day`` lies within [today - 2 years, today + 10 years]."""
    reference = today if today is not None else date.today()
    earliest = _shift_years(reference, -TAG_DATE_PAST_YEARS)
    latest = _shift_years(reference, TAG_DATE_FUTURE_YEARS)
    return earliest <= day <= latest


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
    # Title prefix for the ICS feed; empty = no prefix (see is_valid_shortcode).
    shortcode: str = ""
    # Display color "#rrggbb"; empty = frontend palette default
    # (see is_valid_source_color).
    color: str = ""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """An event as read back from storage, with source metadata attached."""

    source_id: int
    source_name: str
    display_mode: str
    event: CalendarEvent
    # Source shortcode (title prefix in the ICS feed); empty = none.
    shortcode: str = ""
    # Source display color "#rrggbb"; empty = frontend palette default.
    color: str = ""
