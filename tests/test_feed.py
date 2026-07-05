"""Tests for the subscribable ICS feed (app/feed.py).

The feed contains only events from sources with display_mode=filtered
(Roland's work/private calendars), passed through the same family
relevance filter as the calendar views. Sources with display_mode=full
(Marina) are excluded — she subscribes to the feed herself.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from app.feed import CALENDAR_NAME, build_feed
from app.models import CalendarEvent
from app.storage import Storage, default_db_path

BERLIN = ZoneInfo("Europe/Berlin")

# Fixed "now" for deterministic windows: sync window is -7/+90 days.
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 5, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 12, 1, tzinfo=UTC)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


def _timed(uid: str, title: str, start: datetime, end: datetime) -> CalendarEvent:
    return CalendarEvent(uid=uid, title=title, start=start, end=end, all_day=False)


def seed(storage: Storage) -> tuple[int, int]:
    """A filtered work source (with shortcode) and a full source (Marina)."""
    work_id = storage.add_source(
        type="caldav", name="Firma", config={}, display_mode="filtered", shortcode="RMV"
    )
    full_id = storage.add_source(
        type="google", name="Marina", config={}, display_mode="full"
    )
    storage.sync_events(
        work_id,
        [
            _timed(
                "work-evening",
                "Kundentermin",
                datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN),
                datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN),
            ),
            _timed(
                "work-daytime",
                "Standup",
                datetime(2026, 7, 10, 10, 0, tzinfo=BERLIN),
                datetime(2026, 7, 10, 10, 30, tzinfo=BERLIN),
            ),
            CalendarEvent(
                uid="work-trip",
                title="Fortbildung Hamburg",
                start=date(2026, 7, 20),
                end=date(2026, 7, 22),
                all_day=True,
            ),
        ],
        WINDOW_START,
        WINDOW_END,
        synced_at=NOW,
    )
    storage.sync_events(
        full_id,
        [
            _timed(
                "marina-evening",
                "Elternabend",
                datetime(2026, 7, 11, 19, 0, tzinfo=BERLIN),
                datetime(2026, 7, 11, 21, 0, tzinfo=BERLIN),
            )
        ],
        WINDOW_START,
        WINDOW_END,
        synced_at=NOW,
    )
    return work_id, full_id


def parse_feed(payload: bytes) -> Calendar:
    return Calendar.from_ical(payload)


def summaries(calendar: Calendar) -> list[str]:
    return [str(component["SUMMARY"]) for component in calendar.walk("VEVENT")]


class TestFeedContent:
    def test_contains_only_family_relevant_filtered_events(
        self, storage: Storage
    ) -> None:
        seed(storage)
        calendar = parse_feed(build_feed(storage, now=NOW))
        titles = summaries(calendar)
        assert "RMV Kundentermin" in titles
        assert "RMV Fortbildung Hamburg" in titles
        assert not any("Standup" in title for title in titles)  # daytime meeting
        assert not any("Elternabend" in title for title in titles)  # full source

    def test_respects_the_persisted_evening_boundary(self, storage: Storage) -> None:
        seed(storage)
        storage.set_setting("evening_boundary", "18:30")
        titles = summaries(parse_feed(build_feed(storage, now=NOW)))
        assert not any("Kundentermin" in title for title in titles)

    def test_title_without_shortcode_stays_unprefixed(self, storage: Storage) -> None:
        work_id, _ = seed(storage)
        storage.update_source(work_id, shortcode="")
        titles = summaries(parse_feed(build_feed(storage, now=NOW)))
        assert "Kundentermin" in titles

    def test_events_outside_the_sync_window_are_excluded(
        self, storage: Storage
    ) -> None:
        work_id, _ = seed(storage)
        storage.sync_events(
            work_id,
            [
                _timed(
                    "work-old",
                    "Alte Dienstreise",
                    datetime(2026, 5, 10, 8, 0, tzinfo=BERLIN),
                    datetime(2026, 5, 12, 20, 0, tzinfo=BERLIN),
                ),
                _timed(
                    "work-far-future",
                    "Ferne Klausur",
                    datetime(2026, 11, 20, 8, 0, tzinfo=BERLIN),
                    datetime(2026, 11, 21, 20, 0, tzinfo=BERLIN),
                ),
            ],
            WINDOW_START,
            WINDOW_END,
            synced_at=NOW,
        )
        titles = summaries(parse_feed(build_feed(storage, now=NOW)))
        assert not any("Alte Dienstreise" in title for title in titles)
        assert not any("Ferne Klausur" in title for title in titles)


class TestFeedFormat:
    def test_calendar_metadata(self, storage: Storage) -> None:
        seed(storage)
        calendar = parse_feed(build_feed(storage, now=NOW))
        assert str(calendar["X-WR-CALNAME"]) == CALENDAR_NAME
        assert "Familienkalender" in str(calendar["PRODID"])
        assert str(calendar["VERSION"]) == "2.0"
        assert calendar["REFRESH-INTERVAL"].to_ical() == b"PT15M"
        assert calendar["REFRESH-INTERVAL"].params.get("VALUE") == "DURATION"
        assert calendar["X-PUBLISHED-TTL"].to_ical() == b"PT15M"

    def test_timed_event_is_serialized_as_utc(self, storage: Storage) -> None:
        seed(storage)
        calendar = parse_feed(build_feed(storage, now=NOW))
        event = next(
            component
            for component in calendar.walk("VEVENT")
            if "Kundentermin" in str(component["SUMMARY"])
        )
        dtstart = event["DTSTART"].dt
        assert isinstance(dtstart, datetime)
        assert dtstart.utcoffset() == timedelta(0)
        assert dtstart == datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        assert event["DTEND"].dt == datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN)
        assert "DTSTAMP" in event

    def test_all_day_event_uses_date_values_with_exclusive_end(
        self, storage: Storage
    ) -> None:
        seed(storage)
        calendar = parse_feed(build_feed(storage, now=NOW))
        event = next(
            component
            for component in calendar.walk("VEVENT")
            if "Fortbildung" in str(component["SUMMARY"])
        )
        assert event["DTSTART"].dt == date(2026, 7, 20)
        assert not isinstance(event["DTSTART"].dt, datetime)
        assert event["DTEND"].dt == date(2026, 7, 22)  # exclusive iCalendar end

    def test_uids_are_stable_and_unique(self, storage: Storage) -> None:
        seed(storage)
        first = [
            str(component["UID"])
            for component in parse_feed(build_feed(storage, now=NOW)).walk("VEVENT")
        ]
        second = [
            str(component["UID"])
            for component in parse_feed(build_feed(storage, now=NOW)).walk("VEVENT")
        ]
        assert first == second  # stable across builds
        assert len(set(first)) == len(first)  # unique per event
        assert all(uid.endswith("@familienkalender") for uid in first)

    def test_empty_feed_is_still_a_valid_calendar(self, storage: Storage) -> None:
        calendar = parse_feed(build_feed(storage, now=NOW))
        assert str(calendar["X-WR-CALNAME"]) == CALENDAR_NAME
        assert list(calendar.walk("VEVENT")) == []
