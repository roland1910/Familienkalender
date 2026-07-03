"""E2E tests for paging, the today button, the view switcher and swipes."""

import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import (
    goto_calendar,
    month_title,
    monday_of_week,
    swipe_horizontally,
)

pytestmark = pytest.mark.e2e


def _next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def test_month_paging_updates_title(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    today = date.today()
    title = page.locator("#period-title")
    page.locator("#btn-next").click()
    expect(title).to_have_text(month_title(_next_month(today)))
    page.locator("#btn-prev").click()
    page.locator("#btn-prev").click()
    expect(title).to_have_text(month_title(_previous_month(today)))


def test_today_button_returns_to_current_period(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    for _ in range(3):
        page.locator("#btn-next").click()
    page.locator("#btn-today").click()
    expect(page.locator("#period-title")).to_have_text(month_title(date.today()))
    expect(page.locator(".day-cell.today")).to_have_count(1)


def test_view_switcher_toggles_between_month_and_week(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator("#btn-month")).to_have_class(re.compile(r"\bactive\b"))
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator(".month-view")).to_have_count(0)
    expect(page.locator("#btn-week")).to_have_class(re.compile(r"\bactive\b"))
    page.locator("#btn-month").click()
    expect(page.locator(".month-view")).to_be_visible()
    expect(page.locator(".week-view")).to_have_count(0)


def test_week_paging_moves_by_seven_days(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    monday = monday_of_week(date.today())
    expect(
        page.locator(f'.week-day-column[data-date="{monday.isoformat()}"]')
    ).to_be_attached()
    page.locator("#btn-next").click()
    next_monday = monday + timedelta(days=7)
    expect(
        page.locator(f'.week-day-column[data-date="{next_monday.isoformat()}"]')
    ).to_be_attached()


def test_touch_swipe_pages_the_calendar(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    today = date.today()
    title = page.locator("#period-title")
    swipe_horizontally(page, -300)  # swipe left → next period
    expect(title).to_have_text(month_title(_next_month(today)))
    swipe_horizontally(page, 300)  # swipe right → back
    expect(title).to_have_text(month_title(today))


def test_vertical_drag_does_not_page(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    title_before = page.locator("#period-title").text_content()
    page.evaluate(
        """() => {
            const element = document.getElementById("calendar");
            const rect = element.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const touchAt = (x, y) =>
                new Touch({ identifier: 1, target: element, clientX: x, clientY: y });
            element.dispatchEvent(new TouchEvent("touchstart", {
                touches: [touchAt(centerX, rect.top + 100)],
                changedTouches: [touchAt(centerX, rect.top + 100)],
                bubbles: true,
            }));
            element.dispatchEvent(new TouchEvent("touchend", {
                touches: [],
                changedTouches: [touchAt(centerX + 70, rect.top + 400)],
                bubbles: true,
            }));
        }"""
    )
    expect(page.locator("#period-title")).to_have_text(title_before or "")
