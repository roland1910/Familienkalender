"""E2E tests for the self-explanatory header controls (Etappe 36).

Roland's feedback from the kiosk: "Funktion nicht selbsterklärend durch das
Icon". The guiding rule now applied throughout the toolbar:

  * a SWITCH (on/off) shows its state through colour — greyed out = off,
    full colour = on (the screensaver toggle),
  * a SELECTION (which view, which period) shows a label per option plus a
    clear highlight of the active one (mode switch, Monat/Woche),
  * the three-state theme control spells its state out as a word.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

MIN_TOUCH_TARGET_PX = 44


def _filter_of(page: Page, selector: str) -> str:
    return page.eval_on_selector(selector, "el => getComputedStyle(el).filter")


def test_mode_buttons_carry_labels_and_highlight_the_active_one(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)

    # Every mode option names itself in German next to its symbol.
    expect(page.locator("#btn-mode-calendar")).to_contain_text("Kalender")
    expect(page.locator("#btn-mode-power")).to_contain_text("Strom")
    expect(page.locator("#btn-mode-weather")).to_contain_text("Wetter")

    # The active option is marked for sighted users (class -> accent fill)
    # and for assistive technology (aria-pressed).
    expect(page.locator("#btn-mode-calendar")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-mode-power")).to_have_attribute("aria-pressed", "false")

    page.locator("#btn-mode-power").click()
    expect(page.locator("#btn-mode-power")).to_have_class(r"mode-button active")
    expect(page.locator("#btn-mode-power")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-mode-calendar")).to_have_attribute("aria-pressed", "false")


def test_period_switch_marks_the_active_period(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator("#btn-month")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-week")).to_have_attribute("aria-pressed", "false")

    page.locator("#btn-week").click()
    expect(page.locator("#btn-week")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-month")).to_have_attribute("aria-pressed", "false")


def test_period_and_mode_switch_are_separate_groups(page: Page, server_url: str) -> None:
    """Monat/Woche (period inside the calendar) and Kalender/Strom/Wetter
    (top-level view) are different things and must not read as one row of
    lookalike buttons."""
    goto_calendar(page, server_url)
    expect(page.locator(".view-switch #btn-month")).to_have_count(1)
    expect(page.locator(".view-switch #btn-week")).to_have_count(1)
    expect(page.locator(".mode-switch #btn-mode-calendar")).to_have_count(1)
    # A visible separator sits between the two groups.
    expect(page.locator(".toolbar-divider")).to_be_visible()


def test_screensaver_toggle_is_grey_when_off_and_coloured_when_on(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)
    toggle = page.locator("#btn-screensaver")
    icon = "#btn-screensaver .btn-icon"

    # Off: same symbol, but desaturated — recognisable from two metres away.
    expect(toggle).to_have_attribute("aria-pressed", "false")
    off_filter = _filter_of(page, icon)
    assert "grayscale" in off_filter, off_filter

    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    on_filter = _filter_of(page, icon)
    assert on_filter != off_filter
    assert "grayscale" not in on_filter, on_filter

    # And back off again.
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    assert "grayscale" in _filter_of(page, icon)


def test_theme_button_spells_out_its_state(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")
    label = page.locator("#btn-theme .btn-label")

    expect(label).to_have_text("Auto")
    button.click()
    expect(label).to_have_text("Hell")
    button.click()
    expect(label).to_have_text("Dunkel")
    button.click()
    expect(label).to_have_text("Auto")


def test_header_controls_stay_touch_sized_on_kiosk_and_phone(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)
    selectors = [
        "#btn-prev", "#btn-today", "#btn-next", "#btn-month", "#btn-week",
        "#btn-mode-calendar", "#btn-mode-power", "#btn-mode-weather",
        "#btn-theme", "#btn-screensaver",
    ]
    for width, height in ((1920, 1080), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        for selector in selectors:
            box = page.locator(selector).bounding_box()
            assert box is not None, selector
            assert box["width"] >= MIN_TOUCH_TARGET_PX, (selector, width, box)
            assert box["height"] >= MIN_TOUCH_TARGET_PX, (selector, width, box)


def test_mode_labels_show_on_the_kiosk_and_collapse_on_a_phone(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)
    label = page.locator("#btn-mode-power .btn-label")

    page.set_viewport_size({"width": 1920, "height": 1080})
    expect(label).to_be_visible()

    # Marina's phone: the labels give way, the symbols stay.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(label).to_be_hidden()
    expect(page.locator("#btn-mode-power .btn-icon")).to_be_visible()
