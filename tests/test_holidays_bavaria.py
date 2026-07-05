"""Tests for the Bavarian public holiday calendar (app/holidays_bavaria.py).

The family relevance filter only restricts events on workdays; weekends and
Bavarian public holidays show everything. This module provides the holiday
calendar without any external dependency: Easter via the Gaussian Easter
formula, fixed-date holidays as a static list.
"""

from datetime import date

from app.holidays_bavaria import easter_sunday, is_bavarian_holiday, is_workday


class TestEasterSunday:
    """Known Easter dates (Gregorian calendar) for surrounding years."""

    def test_easter_2024(self) -> None:
        assert easter_sunday(2024) == date(2024, 3, 31)

    def test_easter_2025(self) -> None:
        assert easter_sunday(2025) == date(2025, 4, 20)

    def test_easter_2026(self) -> None:
        assert easter_sunday(2026) == date(2026, 4, 5)

    def test_easter_2027(self) -> None:
        assert easter_sunday(2027) == date(2027, 3, 28)

    def test_easter_edge_years(self) -> None:
        # Extremes of the Gregorian cycle: earliest possible (March 22, 1818)
        # and latest possible (April 25, 1943) Easter dates.
        assert easter_sunday(1818) == date(1818, 3, 22)
        assert easter_sunday(1943) == date(1943, 4, 25)


class TestBavarianHolidays2026:
    """Every Bavarian public holiday of 2026, explicitly."""

    def test_all_holidays_2026(self) -> None:
        expected = [
            date(2026, 1, 1),  # Neujahr
            date(2026, 1, 6),  # Heilige Drei Koenige
            date(2026, 4, 3),  # Karfreitag
            date(2026, 4, 6),  # Ostermontag
            date(2026, 5, 1),  # Tag der Arbeit
            date(2026, 5, 14),  # Christi Himmelfahrt (Ostern + 39)
            date(2026, 5, 25),  # Pfingstmontag (Ostern + 50)
            date(2026, 6, 4),  # Fronleichnam (Ostern + 60)
            date(2026, 8, 15),  # Mariae Himmelfahrt
            date(2026, 10, 3),  # Tag der Deutschen Einheit
            date(2026, 11, 1),  # Allerheiligen
            date(2026, 12, 25),  # 1. Weihnachtsfeiertag
            date(2026, 12, 26),  # 2. Weihnachtsfeiertag
        ]
        for day in expected:
            assert is_bavarian_holiday(day), f"{day} must be a Bavarian holiday"

    def test_non_holidays_2026(self) -> None:
        not_holidays = [
            date(2026, 8, 8),  # Augsburger Friedensfest (Augsburg only)
            date(2026, 10, 31),  # Reformationstag (not in Bavaria)
            date(2026, 12, 24),  # Heiligabend (not a public holiday)
            date(2026, 12, 31),  # Silvester (not a public holiday)
            date(2026, 4, 5),  # Ostersonntag (Sunday anyway, not listed)
            date(2026, 5, 24),  # Pfingstsonntag (Sunday anyway, not listed)
            date(2026, 2, 17),  # Faschingsdienstag (not a public holiday)
            date(2026, 7, 6),  # random Monday
        ]
        for day in not_holidays:
            assert not is_bavarian_holiday(day), f"{day} must not be a holiday"

    def test_movable_holidays_2025(self) -> None:
        # Easter 2025 = April 20: cross-check the movable feasts of a
        # second year so the offsets are not accidentally tuned to 2026.
        assert is_bavarian_holiday(date(2025, 4, 18))  # Karfreitag
        assert is_bavarian_holiday(date(2025, 4, 21))  # Ostermontag
        assert is_bavarian_holiday(date(2025, 5, 29))  # Christi Himmelfahrt
        assert is_bavarian_holiday(date(2025, 6, 9))  # Pfingstmontag
        assert is_bavarian_holiday(date(2025, 6, 19))  # Fronleichnam


class TestIsWorkday:
    def test_regular_weekdays_are_workdays(self) -> None:
        assert is_workday(date(2026, 7, 6))  # Monday
        assert is_workday(date(2026, 7, 10))  # Friday

    def test_weekend_days_are_not_workdays(self) -> None:
        assert not is_workday(date(2026, 7, 11))  # Saturday
        assert not is_workday(date(2026, 7, 12))  # Sunday

    def test_weekday_holiday_is_not_a_workday(self) -> None:
        assert not is_workday(date(2026, 6, 4))  # Fronleichnam, a Thursday
        assert not is_workday(date(2026, 1, 1))  # Neujahr, a Thursday

    def test_holiday_falling_on_a_weekend_is_not_a_workday(self) -> None:
        # Bavaria has no substitute-day rule: a holiday on a weekend simply
        # coincides with the weekend, the day stays a non-workday.
        assert not is_workday(date(2026, 10, 3))  # Deutsche Einheit, Saturday
        assert not is_workday(date(2026, 11, 1))  # Allerheiligen, Sunday

    def test_day_before_weekend_holiday_is_still_a_workday(self) -> None:
        assert is_workday(date(2026, 10, 2))  # Friday before Oct 3
