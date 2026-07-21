"""E2E tests for the header controls (Etappe 36 revised in Etappe 37).

Roland's kiosk feedback went a full circle: Etappe 36 added words to the
buttons, Etappe 37 takes them straight back out. The rule now:

  * every control is ICON-ONLY, in the plain uniform blue-button look of
    Monat/Woche (outlined surface tile; the active one filled with the
    accent colour) — no words at all,
  * a SWITCH (the screensaver toggle) shows its state through colour on the
    icon — greyed out = off, full colour = on,
  * the three-state theme control uses three DISTINCT symbols per state,
  * the German name lives in aria-label for assistive tech, never as visible
    text next to the symbol.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

MIN_TOUCH_TARGET_PX = 44

# Words that must NOT appear on the redesigned icon-only controls.
FORBIDDEN_WORDS = ["Kalender", "Strom", "Wetter", "Auto", "Hell", "Dunkel", "Diashow"]


def _filter_of(page: Page, selector: str) -> str:
    return page.eval_on_selector(selector, "el => getComputedStyle(el).filter")


def _bg_of(page: Page, selector: str) -> str:
    return page.eval_on_selector(selector, "el => getComputedStyle(el).backgroundColor")


def test_mode_buttons_are_icon_only_and_highlight_the_active_one(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)

    # No visible words on the mode buttons — only their symbol (an emoji,
    # which carries no A-Z/German letters).
    for selector in ["#btn-mode-calendar", "#btn-mode-power", "#btn-mode-weather"]:
        text = page.locator(selector).inner_text()
        assert not any(ch.isalpha() for ch in text), (selector, text)

    # The German name is announced via aria-label instead.
    expect(page.locator("#btn-mode-calendar")).to_have_attribute("aria-label", "Kalender")
    expect(page.locator("#btn-mode-power")).to_have_attribute("aria-label", "Strom")
    expect(page.locator("#btn-mode-weather")).to_have_attribute("aria-label", "Wetter")

    # The active option is marked for sighted users (class -> accent fill)
    # and for assistive technology (aria-pressed).
    expect(page.locator("#btn-mode-calendar")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-mode-power")).to_have_attribute("aria-pressed", "false")

    page.locator("#btn-mode-power").click()
    expect(page.locator("#btn-mode-power")).to_have_class(r"mode-button active")
    expect(page.locator("#btn-mode-power")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-mode-calendar")).to_have_attribute("aria-pressed", "false")


def test_active_mode_button_is_filled_with_the_accent_colour(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    # The active mode button uses the accent fill; an inactive one uses the
    # plain surface — so the two differ, which is how the current view reads.
    active_bg = _bg_of(page, "#btn-mode-calendar")
    inactive_bg = _bg_of(page, "#btn-mode-power")
    assert active_bg != inactive_bg, (active_bg, inactive_bg)


def test_no_control_shows_any_word(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    slot_text = page.locator("#mode-slot").inner_text()
    for word in FORBIDDEN_WORDS:
        assert word not in slot_text, (word, slot_text)


def test_period_switch_marks_the_active_period(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    expect(page.locator("#btn-month")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-week")).to_have_attribute("aria-pressed", "false")

    page.locator("#btn-week").click()
    expect(page.locator("#btn-week")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#btn-month")).to_have_attribute("aria-pressed", "false")


def test_period_and_mode_switch_are_separate_groups(page: Page, server_url: str) -> None:
    """Monat/Woche (period inside the calendar) and the mode icons are
    different things and stay visually separated."""
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

    # Off: the symbol is desaturated — recognisable from two metres away.
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


def test_theme_button_shows_a_distinct_symbol_per_state(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")
    icon = page.locator("#btn-theme .btn-icon")

    def symbol() -> str:
        return icon.inner_text().strip()

    # Auto, then light, then dark — three visibly different glyphs, and the
    # state is spelled out only in aria-label (never as visible text).
    auto = symbol()
    expect(button).to_have_attribute("aria-label", "Farbschema: automatisch")
    button.click()
    light = symbol()
    expect(button).to_have_attribute("aria-label", "Farbschema: hell")
    button.click()
    dark = symbol()
    expect(button).to_have_attribute("aria-label", "Farbschema: dunkel")
    assert len({auto, light, dark}) == 3, (auto, light, dark)


def test_dark_mode_controls_stay_dark_not_white(page: Page, server_url: str) -> None:
    """Regression guard (Etappe 37): in dark mode the mode buttons and the
    standalone icon buttons must render on a dark surface — never the light
    UA button background that a stale kiosk cache once showed as white."""
    page.emulate_media(color_scheme="dark")
    goto_calendar(page, server_url)

    def luminance(selector: str) -> float:
        rgb = page.eval_on_selector(
            selector,
            """el => {
                const c = getComputedStyle(el).backgroundColor
                    .match(/\\d+/g).map(Number);
                return 0.299 * c[0] + 0.587 * c[1] + 0.699 * c[2];
            }""",
        )
        return float(rgb)

    # Inactive controls sit on the dark surface; a white box would be ~255.
    for selector in ["#btn-mode-power", "#btn-theme", "#btn-screensaver"]:
        assert luminance(selector) < 80, (selector, luminance(selector))


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
