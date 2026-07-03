"""E2E tests for the error/loading states and both target viewports."""

from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.helpers import goto_calendar, month_title

pytestmark = pytest.mark.e2e


def test_failed_fetch_shows_stale_badge_and_keeps_data(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator("#status-badge")).to_be_hidden()
    page.route("**/api/events*", lambda route: route.abort())
    page.locator("#btn-next").click()
    badge = page.locator("#status-badge")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("Daten nicht aktuell")
    # The last known data stays on screen instead of a blank calendar.
    expect(page.locator(".month-grid")).to_be_visible()
    # Once the backend is reachable again, the badge disappears.
    page.unroute("**/api/events*")
    page.locator("#btn-today").click()
    expect(badge).to_be_hidden()
    expect(page.locator("#period-title")).to_have_text(month_title(date.today()))


def test_loading_indicator_shows_before_first_data(page: Page, server_url: str) -> None:
    # Hold the first events request so the loading state is observable,
    # then release it and expect the calendar to replace the indicator.
    pending: list = []
    page.route("**/api/events*", lambda route: pending.append(route))
    page.goto(server_url)
    expect(page.locator("#loading")).to_be_visible()
    while not pending:
        page.wait_for_timeout(25)
    pending[0].continue_()
    page.unroute("**/api/events*")
    expect(page.locator(".month-grid")).to_be_visible()
    expect(page.locator("#loading")).to_have_count(0)


def _assert_no_horizontal_overflow(page: Page) -> None:
    has_overflow = page.evaluate(
        "document.documentElement.scrollWidth > window.innerWidth"
    )
    assert not has_overflow, "page overflows horizontally"


def test_kiosk_viewport_1920x1080(
    browser: Browser, server_url: str, artifacts_dir: Path
) -> None:
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, has_touch=True)
    page = context.new_page()
    try:
        goto_calendar(page, server_url)
        expect(page.locator(".month-grid")).to_be_visible()
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "month-1920x1080.png")
        page.locator("#btn-week").click()
        expect(page.locator(".week-view")).to_be_visible()
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "week-1920x1080.png")
    finally:
        context.close()


def test_ingress_panel_viewport_800x1280(
    browser: Browser, server_url: str, artifacts_dir: Path
) -> None:
    """Narrow HA ingress side panel: the <=900px breakpoint must kick in."""
    context = browser.new_context(viewport={"width": 800, "height": 1280}, has_touch=True)
    page = context.new_page()
    try:
        goto_calendar(page, server_url)
        expect(page.locator(".month-grid")).to_be_visible()
        chip = page.locator(".chip", has_text="Zahnarzt Emil")
        expect(chip).to_be_visible()
        # Breakpoint behavior: chip times are hidden below 900px width.
        expect(chip.locator(".chip-time")).to_have_css("display", "none")
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "month-800x1280.png")
        page.locator("#btn-week").click()
        expect(page.locator(".week-view")).to_be_visible()
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "week-800x1280.png")
    finally:
        context.close()


def test_narrow_viewport_1280x720(
    browser: Browser, server_url: str, artifacts_dir: Path
) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 720}, has_touch=True)
    page = context.new_page()
    try:
        goto_calendar(page, server_url)
        expect(page.locator(".month-grid")).to_be_visible()
        expect(page.locator(".chip", has_text="Zahnarzt Emil")).to_be_visible()
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "month-1280x720.png")
        page.locator("#btn-week").click()
        expect(page.locator(".week-view")).to_be_visible()
        _assert_no_horizontal_overflow(page)
        page.screenshot(path=artifacts_dir / "week-1280x720.png")
    finally:
        context.close()
