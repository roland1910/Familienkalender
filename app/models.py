"""Shared domain models for calendar sources and events."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

# The family lives in Germany; all display and filtering decisions are made
# in this timezone. Will become configurable with the admin UI if ever needed.
LOCAL_TZ = ZoneInfo("Europe/Berlin")

# "google": Google Calendar; "caldav": Nextcloud/generic CalDAV;
# "google_contacts": contact birthdays via the Google People API (see
# app/sources/google_contacts.py).
SOURCE_TYPES = ("google", "caldav", "google_contacts")
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


# Per-source precedence for collapsing duplicate events in the ICS feed:
# when the same appointment appears in several feed sources, the one from
# the source with the HIGHER priority survives. A plain signed integer in a
# small, sane range — negative values push a source below the default.
FEED_PRIORITY_MIN = -100
FEED_PRIORITY_MAX = 100


def is_valid_feed_priority(value: int) -> bool:
    """Whether ``value`` is an acceptable feed priority (bounded integer)."""
    # The bool guard only protects the direct storage path (create/update
    # source with a raw Python value): a stray True/False must not be written
    # as 1/0. On the HTTP path Pydantic already coerces bool→int before this
    # check ever runs, so the guard is defence-in-depth for callers that reach
    # storage without passing through the request models.
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and FEED_PRIORITY_MIN <= value <= FEED_PRIORITY_MAX
    )


# Fixed, neutral title of the "Busy MV" blocks the add-on writes into Roland's
# Xalt calendar (see app.google_busy). Single source of truth for the write
# body AND for both read clients: the Google and CalDAV readers skip
# appointments with this title so a self-created block that returns via Roland's
# external Xalt->MoreValue sync is never read back (loop guard).
BUSY_BLOCK_TITLE = "Busy MV"


def is_busy_block_title(title: str | None) -> bool:
    """Whether ``title`` is exactly the "Busy MV" block title (normalized).

    Comparison trims outer whitespace and ignores case. A title that merely
    contains "Busy MV" (e.g. "Busy MV Vorbereitung") is a real appointment and
    must NOT match — only the exact normalized title does.
    """
    if not title:
        return False
    return title.strip().casefold() == BUSY_BLOCK_TITLE.casefold()


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
    # Whether this source's (family-relevant) events appear in the
    # subscribable ICS feed. Independent of display_mode; the historical
    # default (filtered sources feed the subscription) is applied when a
    # source is created and by the storage migration.
    include_in_feed: bool = False
    # Precedence when the ICS feed collapses duplicate events across
    # sources (higher wins; tie broken by lower source id). Default 0 —
    # see is_valid_feed_priority.
    feed_priority: int = 0


@dataclass(frozen=True, slots=True)
class BusyBlock:
    """One "Busy MV" block the add-on maintains in Roland's Xalt calendar.

    Maps a source event (identified by ``source_key`` = source_id|uid|start,
    the same identity the events table uses) to the Google event id of the
    block that mirrors it, plus the block's current time range. This mapping
    lets the busy-sync diff decide precisely which blocks to create, patch or
    delete — every write targets a known, self-created event id, never a
    foreign calendar entry.
    """

    source_key: str
    google_event_id: str
    start: datetime | date
    end: datetime | date
    all_day: bool


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
    # Whether the source participates in the subscribable ICS feed.
    include_in_feed: bool = False
    # Source precedence for de-duplicating events in the ICS feed.
    feed_priority: int = 0
