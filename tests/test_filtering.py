"""Tests for the family relevance filter.

Sources with display_mode=filtered only show events that matter to the
family: events reaching into (or lying in) the evening, multi-day events,
and all-day events. Plain intra-day meetings are hidden. All comparisons
happen in the local timezone (Europe/Berlin), regardless of the timezone
the event was delivered in.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.filtering import DEFAULT_EVENING_BOUNDARY, filter_events, is_family_relevant
from app.models import CalendarEvent

BERLIN = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


def timed(start: datetime, end: datetime, uid: str = "uid") -> CalendarEvent:
    return CalendarEvent(uid=uid, title="Termin", start=start, end=end, all_day=False)


def all_day(start: date, end: date, uid: str = "uid") -> CalendarEvent:
    return CalendarEvent(uid=uid, title="Ganztägig", start=start, end=end, all_day=True)


class TestSameDayTimedEvents:
    def test_morning_meeting_is_dropped(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 10, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 11, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is False

    def test_event_ending_exactly_at_boundary_is_dropped(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 16, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 17, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is False

    def test_event_ending_just_after_boundary_is_shown(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 16, 59, tzinfo=BERLIN),
            datetime(2026, 7, 6, 17, 1, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True

    def test_event_starting_after_boundary_is_shown(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 19, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 20, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True

    def test_event_ending_exactly_at_midnight_is_shown(self) -> None:
        # Ends at midnight sharp (exclusive end): still a same-day event,
        # but it clearly reaches into the evening.
        event = timed(
            datetime(2026, 7, 6, 22, 0, tzinfo=BERLIN),
            datetime(2026, 7, 7, 0, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True

    def test_event_spanning_the_whole_workday_is_shown(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 8, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 18, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True


class TestMultiDayEvents:
    def test_overnight_event_is_shown(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 22, 0, tzinfo=BERLIN),
            datetime(2026, 7, 7, 6, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True

    def test_overnight_event_starting_before_noon_is_shown(self) -> None:
        # Business trip: leaves Monday 09:00, returns Tuesday 12:00.
        event = timed(
            datetime(2026, 7, 6, 9, 0, tzinfo=BERLIN),
            datetime(2026, 7, 7, 12, 0, tzinfo=BERLIN),
        )
        assert is_family_relevant(event) is True

    def test_multi_day_all_day_event_is_shown(self) -> None:
        event = all_day(date(2026, 7, 6), date(2026, 7, 9))
        assert is_family_relevant(event) is True


class TestAllDaySingleDay:
    def test_single_day_all_day_event_is_shown(self) -> None:
        # Decision: single-day all-day events are shown — they carry
        # family-relevant info (holidays, birthdays) and are rare in
        # work calendars.
        event = all_day(date(2026, 7, 6), date(2026, 7, 7))
        assert is_family_relevant(event) is True


class TestTimezoneHandling:
    def test_utc_event_reaching_into_local_evening_is_shown(self) -> None:
        # 14:00-16:30 UTC in summer = 16:00-18:30 Berlin: reaches the evening.
        event = timed(
            datetime(2026, 7, 6, 14, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 16, 30, tzinfo=UTC),
        )
        assert is_family_relevant(event) is True

    def test_utc_event_ending_before_local_boundary_is_dropped(self) -> None:
        # 13:00-15:00 UTC in summer = 15:00-17:00 Berlin: ends at the boundary.
        event = timed(
            datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 15, 0, tzinfo=UTC),
        )
        assert is_family_relevant(event) is False

    def test_foreign_timezone_is_converted(self) -> None:
        # 11:00-11:30 New York in summer = 17:00-17:30 Berlin.
        event = timed(
            datetime(2026, 7, 6, 11, 0, tzinfo=NEW_YORK),
            datetime(2026, 7, 6, 11, 30, tzinfo=NEW_YORK),
        )
        assert is_family_relevant(event) is True

    def test_utc_event_crossing_midnight_only_in_utc_is_dropped(self) -> None:
        # 23:00-23:59 UTC on the 5th = 01:00-01:59 Berlin on the 6th:
        # a single local calendar day, ends before the boundary.
        event = timed(
            datetime(2026, 7, 5, 23, 0, tzinfo=UTC),
            datetime(2026, 7, 5, 23, 59, tzinfo=UTC),
        )
        assert is_family_relevant(event) is False


class TestDaylightSavingTransitions:
    def test_spring_forward_day_event_in_local_evening_is_shown(self) -> None:
        # 2026-03-29 is the CET→CEST switch: 15:30 UTC = 17:30 CEST.
        event = timed(
            datetime(2026, 3, 29, 15, 30, tzinfo=UTC),
            datetime(2026, 3, 29, 16, 30, tzinfo=UTC),
        )
        assert is_family_relevant(event) is True

    def test_fall_back_day_event_before_boundary_is_dropped(self) -> None:
        # 2026-10-25 is the CEST→CET switch: 15:00-16:00 UTC = 16:00-17:00 CET.
        event = timed(
            datetime(2026, 10, 25, 15, 0, tzinfo=UTC),
            datetime(2026, 10, 25, 16, 0, tzinfo=UTC),
        )
        assert is_family_relevant(event) is False

    def test_fall_back_day_event_after_boundary_is_shown(self) -> None:
        # 16:30 UTC = 17:30 CET after the switch.
        event = timed(
            datetime(2026, 10, 25, 16, 30, tzinfo=UTC),
            datetime(2026, 10, 25, 17, 30, tzinfo=UTC),
        )
        assert is_family_relevant(event) is True


class TestConfigurableBoundary:
    def test_custom_boundary_moves_the_cut(self) -> None:
        event = timed(
            datetime(2026, 7, 6, 17, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 17, 30, tzinfo=BERLIN),
        )
        assert is_family_relevant(event, boundary=time(18, 0)) is False
        assert is_family_relevant(event, boundary=time(17, 0)) is True

    def test_default_boundary_is_17_00(self) -> None:
        assert time(17, 0) == DEFAULT_EVENING_BOUNDARY


class TestFilterEvents:
    def test_full_mode_keeps_everything(self) -> None:
        morning = timed(
            datetime(2026, 7, 6, 10, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 11, 0, tzinfo=BERLIN),
            uid="morning",
        )
        assert filter_events([morning], display_mode="full") == [morning]

    def test_filtered_mode_drops_intra_day_events(self) -> None:
        morning = timed(
            datetime(2026, 7, 6, 10, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 11, 0, tzinfo=BERLIN),
            uid="morning",
        )
        evening = timed(
            datetime(2026, 7, 6, 18, 0, tzinfo=BERLIN),
            datetime(2026, 7, 6, 19, 0, tzinfo=BERLIN),
            uid="evening",
        )
        result = filter_events([morning, evening], display_mode="filtered")
        assert [event.uid for event in result] == ["evening"]
