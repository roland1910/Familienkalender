"""E2E tests for the per-device view persistence (localStorage).

View (month/week), displayed period (anchor) and mode (calendar/power)
survive a reload; broken localStorage values fall back to the defaults.
Each test runs in a fresh browser context, so localStorage starts empty.
"""

import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar, monday_of_week, month_title

pytestmark = pytest.mark.e2e

ACTIVE = re.compile(r"\bactive\b")


def _week_column(page: Page, day: date):
    return page.locator(f'.week-day-column[data-date="{day.isoformat()}"]')


def test_week_view_and_paged_week_survive_reload(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    page.locator("#btn-next").click()
    next_monday = monday_of_week(date.today()) + timedelta(days=7)
    expect(_week_column(page, next_monday)).to_be_attached()

    page.reload()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator("#btn-week")).to_have_class(ACTIVE)
    expect(_week_column(page, next_monday)).to_be_attached()


def test_today_button_resets_the_saved_anchor(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    page.locator("#btn-next").click()
    page.locator("#btn-next").click()
    page.locator("#btn-today").click()
    monday = monday_of_week(date.today())
    expect(_week_column(page, monday)).to_be_attached()

    # A later reload must NOT jump back to the previously paged week.
    page.reload()
    expect(page.locator(".week-view")).to_be_visible()
    expect(_week_column(page, monday)).to_be_attached()


def test_month_week_switch_persists_across_reloads(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    page.reload()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator("#btn-week")).to_have_class(ACTIVE)

    page.locator("#btn-month").click()
    expect(page.locator(".month-view")).to_be_visible()
    page.reload()
    expect(page.locator(".month-view")).to_be_visible()
    expect(page.locator("#btn-month")).to_have_class(ACTIVE)
    expect(page.locator("#period-title")).to_have_text(month_title(date.today()))


def test_power_mode_persists_across_reload(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-mode-power").click()
    expect(page.locator("#power")).to_be_visible()

    page.reload()
    expect(page.locator("#power")).to_be_visible()
    expect(page.locator("#btn-mode-power")).to_have_class(ACTIVE)
    expect(page.locator("#period-title")).to_have_text("Strom")
    expect(page.locator("#calendar")).to_be_hidden()
    # Switching back to the calendar keeps working after the restore.
    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_broken_localstorage_falls_back_to_defaults(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.evaluate(
        """() => localStorage.setItem(
            "familienkalender.view-state.v1",
            '{"view":"hacked","anchor":"2026-99-99","mode":7}'
        )"""
    )
    page.reload()
    # Defaults: month view of the current month, calendar mode, no crash.
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator("#btn-month")).to_have_class(ACTIVE)
    expect(page.locator("#period-title")).to_have_text(month_title(date.today()))
