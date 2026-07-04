"""E2E tests for day tags: picker UI, cell display, removal, persistence."""

from datetime import date, timedelta

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar, goto_week_containing

pytestmark = pytest.mark.e2e


def open_day_popover(page: Page, day: date) -> None:
    """Open the day popover by tapping the day number in the month cell."""
    page.locator(f'.day-cell[data-date="{day.isoformat()}"] .day-number').click()
    expect(page.locator(".popover-panel")).to_be_visible()


def close_popover(page: Page) -> None:
    page.locator(".popover-close").click()
    expect(page.locator(".popover-panel")).to_have_count(0)


def test_set_tag_via_picker_remove_and_persist(page: Page, server_url: str) -> None:
    day = date.today()
    cell = page.locator(f'.day-cell[data-date="{day.isoformat()}"]')

    # Add a tag through the picker.
    goto_calendar(page, server_url)
    open_day_popover(page, day)
    picker = page.locator(".popover-tags")
    expect(picker).to_be_visible()
    picker.locator(".tag-button.tag-option", has_text="😀").click()
    expect(picker.locator(".tag-button.tag-current", has_text="😀")).to_be_visible()
    close_popover(page)
    expect(cell.locator(".day-tags")).to_have_text("😀")

    # Survives a full page reload (stored on the server).
    goto_calendar(page, server_url)
    expect(cell.locator(".day-tags")).to_have_text("😀")

    # Tapping the current tag removes it again.
    open_day_popover(page, day)
    page.locator(".popover-tags .tag-button.tag-current", has_text="😀").click()
    expect(page.locator(".popover-tags .tag-button.tag-current")).to_have_count(0)
    close_popover(page)
    expect(cell.locator(".day-tags")).to_have_count(0)


def test_multiple_tags_and_cap_disables_adding(page: Page, server_url: str) -> None:
    day = date.today() + timedelta(days=1)
    goto_calendar(page, server_url)
    open_day_popover(page, day)
    picker = page.locator(".popover-tags")
    for emoji in ("⭐", "🎉", "🎂"):
        picker.locator(".tag-button.tag-option", has_text=emoji).click()
        expect(picker.locator(".tag-button.tag-current", has_text=emoji)).to_be_visible()
    # Cap reached: the remaining options are disabled.
    expect(picker.locator(".tag-button.tag-option").first).to_be_disabled()
    close_popover(page)
    cell = page.locator(f'.day-cell[data-date="{day.isoformat()}"]')
    expect(cell.locator(".day-tags")).to_have_text("⭐🎉🎂")

    # Clean up so other tests see a pristine day.
    open_day_popover(page, day)
    for emoji in ("⭐", "🎉", "🎂"):
        page.locator(".popover-tags .tag-button.tag-current", has_text=emoji).click()
        expect(
            page.locator(".popover-tags .tag-button.tag-current", has_text=emoji)
        ).to_have_count(0)
    close_popover(page)


def test_week_view_shows_tags_in_column_header(page: Page, server_url: str) -> None:
    day = date.today() + timedelta(days=2)
    response = httpx.put(
        f"{server_url}/api/tags/{day.isoformat()}", json={"emojis": ["🌞"]}, timeout=5.0
    )
    assert response.status_code == 200
    try:
        goto_week_containing(page, server_url, day)
        label = page.locator(".week-day-label", has_text="🌞")
        expect(label).to_be_visible()
        # The column header also opens the picker for that day.
        label.click()
        expect(page.locator(".popover-tags .tag-button.tag-current", has_text="🌞")).to_be_visible()
    finally:
        httpx.put(f"{server_url}/api/tags/{day.isoformat()}", json={"emojis": []}, timeout=5.0)
