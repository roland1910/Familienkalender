"""E2E tests for the month view (grid, chips, overflow popover, XSS safety)."""

import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import (
    MONTH_NAMES_DE,
    goto_calendar,
    goto_month_containing,
    month_grid_range,
)

pytestmark = pytest.mark.e2e


def test_month_grid_renders_with_german_weekdays(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator(".month-grid")).to_be_visible()
    weekdays = page.locator(".weekday-header .weekday")
    expect(weekdays).to_have_count(7)
    expect(weekdays.first).to_have_text("Mo")
    expect(weekdays.last).to_have_text("So")
    expect(page.locator(".day-cell")).to_have_count(42)


def test_period_title_shows_german_month_and_year(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    today = date.today()
    expected = f"{MONTH_NAMES_DE[today.month - 1]} {today.year}"
    expect(page.locator("#period-title")).to_have_text(expected)


def test_today_is_highlighted(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    today_cell = page.locator(f'.day-cell[data-date="{date.today().isoformat()}"]')
    expect(today_cell).to_have_class(re.compile(r"\btoday\b"))


def test_timed_event_chip_shows_time_and_title(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    chip = page.locator(".chip", has_text="Zahnarzt Emil")
    expect(chip).to_be_visible()
    expect(chip).to_contain_text("15:00")


def test_multi_day_event_appears_on_each_visible_day(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    today = date.today()
    event_days = [today + timedelta(days=offset) for offset in (5, 6, 7)]
    grid_start, grid_end = month_grid_range(today)
    visible_days = [day for day in event_days if grid_start <= day <= grid_end]
    chips = page.locator(".chip", has_text="Oma & Opa zu Besuch")
    expect(chips).to_have_count(len(visible_days))


def test_overflow_day_shows_more_button_and_popover(page: Page, server_url: str) -> None:
    overflow_day = date.today() + timedelta(days=3)
    goto_month_containing(page, server_url, overflow_day)
    cell = page.locator(f'.day-cell[data-date="{overflow_day.isoformat()}"]')
    more = cell.locator(".more-button")
    expect(more).to_be_visible()
    assert "weitere" in (more.text_content() or "")
    more.click()
    popover = page.locator("#day-popover")
    expect(popover).to_be_visible()
    for index in range(1, 7):
        expect(popover).to_contain_text(f"Demo-Termin {index}")
    popover.locator(".popover-close").click()
    expect(popover).to_be_hidden()


def test_xss_payload_titles_render_as_plain_text(page: Page, server_url: str) -> None:
    """Hostile event titles must appear literally, never as markup (rule 4)."""
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    xss_day = date.today() + timedelta(days=1)
    goto_month_containing(page, server_url, xss_day)
    img_chip = page.locator(".chip", has_text="Böser Termin")
    expect(img_chip).to_be_visible()
    assert '<img src=x onerror=alert("xss")>' in (img_chip.text_content() or "")
    script_chip = page.locator(".chip", has_text="Skript-Termin")
    expect(script_chip).to_be_visible()
    assert '<script>alert("xss")</script>' in (script_chip.text_content() or "")
    # The payload never became DOM: no injected image, no executed alert.
    assert page.locator('img[src="x"]').count() == 0
    assert dialogs == []
