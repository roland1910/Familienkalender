"""E2E tests for the source legend below the calendar.

The legend shows one dot+name pair per enabled source, using the same
color mapping as the event chips (colorForSource in colors.js).
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

# Demo sources (scripts/seed_demo.py) get ids 1..3 in insert order; the
# palette in colors.js maps id % 8 to these colors.
EXPECTED_LEGEND_COLORS = {
    "Marina": "rgb(217, 119, 6)",  # id 1 -> #d97706
    "Kunde": "rgb(5, 150, 105)",  # id 2 -> #059669
    "Firma": "rgb(220, 38, 38)",  # id 3 -> #dc2626
}


def _legend_dot_color(page: Page, name: str) -> str:
    item = page.locator(".legend-item", has_text=name)
    expect(item).to_be_visible()
    return item.locator(".legend-dot").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    )


def test_legend_shows_all_sources_with_chip_colors(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    legend = page.locator("#legend")
    expect(legend).to_be_visible()
    expect(legend.locator(".legend-item")).to_have_count(3)
    for name, expected_color in EXPECTED_LEGEND_COLORS.items():
        assert _legend_dot_color(page, name) == expected_color


def test_legend_color_matches_the_source_chip(page: Page, server_url: str) -> None:
    """The legend must use the exact chip color mapping, not its own."""
    goto_calendar(page, server_url)
    chip_color = page.locator(".chip", has_text="Zahnarzt Emil").evaluate(
        "node => getComputedStyle(node).backgroundColor"
    )
    assert _legend_dot_color(page, "Marina") == chip_color


def test_legend_is_visible_in_week_view(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-week").click()
    expect(page.locator(".week-view")).to_be_visible()
    expect(page.locator("#legend")).to_be_visible()
    expect(page.locator("#legend .legend-item", has_text="Marina")).to_be_visible()


def test_legend_is_hidden_in_power_view(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator("#legend")).to_be_visible()
    page.locator("#btn-mode-power").click()
    expect(page.locator("#legend")).to_be_hidden()
    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#legend")).to_be_visible()
