"""E2E tests for the week view (time grid, positioned events, all-day bars)."""

from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar, goto_week_containing

pytestmark = pytest.mark.e2e

HOUR_HEIGHT_PX = 60


def test_week_view_shows_seven_columns_and_time_grid(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator(".week-day-column")).to_have_count(7)
    labels = page.locator(".week-day-label")
    expect(labels).to_have_count(7)
    expect(labels.first).to_contain_text("Mo")
    expect(page.locator(".hour-label", has_text="08:00")).to_be_attached()
    expect(page.locator("#period-title")).to_contain_text("KW")


def test_night_hours_are_collapsed_without_early_events(
    page: Page, server_url: str
) -> None:
    """No demo event in the current week starts before 08:00, so the grid
    hides 00:00-08:00 entirely and starts at 08:00 (replaces the old,
    flaky initial-scroll-to-morning mechanism)."""
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    labels = page.locator(".hour-label")
    expect(labels.first).to_have_text("08:00")
    expect(page.locator(".hour-label", has_text="07:00")).to_have_count(0)
    expect(labels).to_have_count(24 - 8)  # 08:00 .. 23:00, evening kept
    grid_height = page.locator(".week-grid").evaluate(
        "node => parseFloat(node.style.height)"
    )
    assert grid_height == (24 - 8) * HOUR_HEIGHT_PX


def test_early_event_expands_the_grid_to_its_full_hour(
    page: Page, server_url: str
) -> None:
    """The demo week three weeks ahead has a 06:30 event: the grid starts
    at 06:00 (full hour of the earliest event) and positions it there."""
    target = date.today() + timedelta(days=21)
    goto_week_containing(page, server_url, target)
    labels = page.locator(".hour-label")
    expect(labels.first).to_have_text("06:00")
    expect(page.locator(".hour-label", has_text="05:00")).to_have_count(0)
    column = page.locator(f'.week-day-column[data-date="{target.isoformat()}"]')
    event = column.locator(".timed-event", has_text="Frühdienst")
    expect(event).to_be_visible()
    top = event.evaluate("node => parseFloat(node.style.top)")
    assert top == 0.5 * HOUR_HEIGHT_PX  # 06:30 in a grid starting at 06:00


def test_timed_event_is_positioned_by_time(page: Page, server_url: str) -> None:
    goto_week_containing(page, server_url, date.today())
    column = page.locator(f'.week-day-column[data-date="{date.today().isoformat()}"]')
    event = column.locator(".timed-event", has_text="Zahnarzt Emil")
    event.scroll_into_view_if_needed()
    expect(event).to_be_visible()
    top = event.evaluate("node => parseFloat(node.style.top)")
    height = event.evaluate("node => parseFloat(node.style.height)")
    assert top == (15 - 8) * HOUR_HEIGHT_PX  # starts 15:00, grid starts 08:00
    assert height == HOUR_HEIGHT_PX  # one hour long


def test_multi_day_event_appears_as_all_day_bar(page: Page, server_url: str) -> None:
    visit_start = date.today() + timedelta(days=5)
    goto_week_containing(page, server_url, visit_start)
    bar = page.locator(".allday-bar", has_text="Oma & Opa zu Besuch")
    expect(bar).to_be_visible()


def test_filtered_daytime_meeting_is_hidden(page: Page, server_url: str) -> None:
    """A plain daytime meeting from a filtered source never reaches the UI,
    while the evening event of the same source is shown."""
    target = date.today() + timedelta(days=1)
    goto_week_containing(page, server_url, target)
    expect(page.locator(".timed-event", has_text="Kundentermin München")).to_be_visible()
    expect(page.get_by_text("Weekly Sync")).to_have_count(0)


def test_sources_get_distinct_deterministic_colors(page: Page, server_url: str) -> None:
    target = date.today() + timedelta(days=1)
    goto_week_containing(page, server_url, target)
    marina_chip = page.locator(".timed-event", has_text="Böser Termin")
    kunde_chip = page.locator(".timed-event", has_text="Kundentermin München")
    marina_color = marina_chip.evaluate("node => getComputedStyle(node).backgroundColor")
    kunde_color = kunde_chip.evaluate("node => getComputedStyle(node).backgroundColor")
    assert marina_color != kunde_color
