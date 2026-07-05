"""Bavarian public holidays and the workday test used by the event filter.

Self-contained on purpose (no `holidays` dependency): the set of Bavarian
public holidays is small, stable, and fully determined by the Gaussian
Easter formula plus a handful of fixed dates.

Included holidays (gesetzliche Feiertage in Bayern):
Neujahr, Heilige Drei Koenige, Karfreitag, Ostermontag, Tag der Arbeit,
Christi Himmelfahrt, Pfingstmontag, Fronleichnam, Mariae Himmelfahrt,
Tag der Deutschen Einheit, Allerheiligen, 1./2. Weihnachtsfeiertag.

Mariae Himmelfahrt (Aug 15) is a public holiday only in predominantly
Catholic Bavarian municipalities; the family lives in one, so it is
included. The Augsburger Friedensfest (Aug 8, Augsburg city only) is not.
"""

from datetime import date, timedelta
from functools import lru_cache


def easter_sunday(year: int) -> date:
    """Easter Sunday (Gregorian calendar) via the Gaussian Easter formula.

    Anonymous Gregorian algorithm (Meeus/Jones/Butcher form of Gauss's
    computus); valid for all Gregorian years.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    day_offset = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * day_offset) // 451
    month, day = divmod(h + day_offset - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=8)
def _holidays_for_year(year: int) -> frozenset[date]:
    """All Bavarian public holidays of one year."""
    easter = easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),  # Neujahr
            date(year, 1, 6),  # Heilige Drei Koenige
            easter - timedelta(days=2),  # Karfreitag
            easter + timedelta(days=1),  # Ostermontag
            date(year, 5, 1),  # Tag der Arbeit
            easter + timedelta(days=39),  # Christi Himmelfahrt
            easter + timedelta(days=50),  # Pfingstmontag
            easter + timedelta(days=60),  # Fronleichnam
            date(year, 8, 15),  # Mariae Himmelfahrt (see module docstring)
            date(year, 10, 3),  # Tag der Deutschen Einheit
            date(year, 11, 1),  # Allerheiligen
            date(year, 12, 25),  # 1. Weihnachtsfeiertag
            date(year, 12, 26),  # 2. Weihnachtsfeiertag
        }
    )


def is_bavarian_holiday(day: date) -> bool:
    """Whether the given local calendar day is a Bavarian public holiday."""
    return day in _holidays_for_year(day.year)


def is_workday(day: date) -> bool:
    """Monday through Friday, and not a Bavarian public holiday.

    Bavaria has no substitute-day rule: a holiday falling on a weekend is
    simply a weekend day, nothing moves.
    """
    return day.weekday() < 5 and not is_bavarian_holiday(day)
