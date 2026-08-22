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


# Marker properties on every VEVENT the add-on mirrors from a Google source
# into a Nextcloud/CalDAV calendar (see app.caldav_write / app.mirror_sync).
# Single source of truth for the write side AND for the CalDAV read client,
# which skips marked components so a self-written copy is never read back
# (duplicate in views/feed, and a mirror loop through the busy sync).
#
# MIRROR_MARKER_PROP carries the source key of the mirrored appointment, so a
# copy is self-describing; MIRROR_OWNER_PROP is a constant-valued flag, so
# "is this ours?" is one exact lookup independent of the key's value.
MIRROR_MARKER_PROP = "X-FAMILIENKALENDER-MIRROR"
MIRROR_OWNER_PROP = "X-FAMILIENKALENDER-OWNER"
MIRROR_OWNER_VALUE = "1"

# Marker property of the yearly birthday series the add-on writes into a
# CalDAV calendar (see app.caldav_write / app.birthday_sync). It carries the
# PERSON key, and the series additionally lives in its own UID namespace, so
# the mirror sync and the birthday sync can never mistake each other's
# objects for their own orphans. MIRROR_OWNER_PROP is deliberately shared by
# both: it is the flag the CalDAV READ client uses to skip everything the
# add-on wrote itself (loop guard), and that applies to both kinds.
BIRTHDAY_MARKER_PROP = "X-FAMILIENKALENDER-BIRTHDAY"


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

    ``description``, ``organizer`` and ``attendees`` are the detail fields the
    mirror sync copies into Roland's MoreValue calendar (Etappe 45, his
    explicit request). All three are plain, already display-ready text —
    ``attendees`` holds one participant per line — rather than structured
    objects: nothing in the add-on reasons ABOUT them, everything just stores,
    compares and renders them, so a single string keeps the model hashable,
    the storage column trivial and the diff a plain comparison. Only the
    Google read client fills them today; every other source leaves them None.
    """

    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    location: str | None = None
    description: str | None = None
    organizer: str | None = None
    attendees: str | None = None

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
class MirrorEvent:
    """One mirrored copy the add-on maintains in the target CalDAV calendar.

    Maps a source event (identified by ``source_key`` = source_id|uid|start,
    the same identity the events table and the busy sync use) to the CalDAV
    resource that mirrors it. ``etag`` is the server's last known validator
    and drives the conditional ``If-Match`` requests; ``title``/``location``,
    the detail fields and the time range are what the diff compares against
    the source event, and the stored title is what the change log shows when
    a copy is deleted (the source appointment is gone by then).

    The detail fields default to None, which reads as "not known yet" — that
    is what makes every copy written before Etappe 45 differ from its source
    exactly once, get rewritten with the details, and then compare equal.

    Every update/delete goes through ``resource_url`` from this table, so a
    write always targets a resource the add-on created itself.
    """

    source_key: str
    resource_url: str
    etag: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    title: str = ""
    location: str | None = None
    description: str | None = None
    organizer: str | None = None
    attendees: str | None = None


# The two calendars the birthday sync can write into. Both are optional and
# switched independently in the admin UI (see app.birthday_sync).
BIRTHDAY_TARGETS = ("google", "caldav")


@dataclass(frozen=True, slots=True)
class BirthdayBlock:
    """One yearly birthday series the add-on maintains in a target calendar.

    Maps a PERSON (``person_key`` = source_id|<contact resource>, deliberately
    year-independent — see app.birthday_sync.person_key) to the remote object
    that represents their birthday: a Google event id or a CalDAV resource
    URL, depending on ``target``. The primary key is the (person, target)
    pair, because the same person is written into both calendars.

    ``start`` is the series' DTSTART date (all-day; a 29 February birthday is
    normalized to the 28th so the yearly recurrence fires every year).
    ``title`` and ``start`` are what the diff compares, and ``etag`` is the
    CalDAV validator driving the conditional requests (empty for Google).
    """

    person_key: str
    target: str
    remote_id: str
    start: date
    title: str = ""
    etag: str = ""


# Change log (Änderungsprotokoll): a small audit trail of what each sync
# changed, in both directions — incoming (source → Familienkalender) and
# outgoing (Belegt-Sync → Xalt). See app.storage (audit_log table),
# app.sync (incoming diff) and app.busy_sync (outgoing writes).
AUDIT_DIRECTIONS = ("in", "out")
AUDIT_ACTIONS = ("added", "updated", "removed")


@dataclass(frozen=True, slots=True)
class EventChange:
    """One incoming event difference detected while syncing a single source.

    ``action`` is one of AUDIT_ACTIONS; ``event_start`` is the storage-encoded
    start (UTC ISO for timed, ISO date for all-day) so the caller can log it
    without re-encoding. A pure time shift of an appointment surfaces as a
    ``removed`` (old start) plus an ``added`` (new start), because the start is
    part of the event identity.
    """

    action: str
    title: str
    event_start: str


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One row of the change log (Änderungsprotokoll).

    ``ts`` is an ISO-8601 UTC timestamp, ``direction`` one of AUDIT_DIRECTIONS,
    ``scope`` the source name (incoming) or target label (outgoing), ``action``
    one of AUDIT_ACTIONS. ``title`` is a foreign string (appointment title) and
    must only ever be rendered via textContent in the frontend.
    """

    ts: str
    direction: str
    scope: str
    action: str
    title: str
    event_start: str | None = None
    details: str | None = None


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
