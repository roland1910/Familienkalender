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

Etappe 38 (photo of the real display): the symbols are inline SVG, NOT
emoji. The kiosk browser (WebKitGTK on HA-OS) has no font for emoji beyond
the BMP, so the calendar/rain/picture glyphs rendered as empty boxes.
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

MIN_TOUCH_TARGET_PX = 44

# Words that must NOT appear on the redesigned icon-only controls.
FORBIDDEN_WORDS = ["Kalender", "Strom", "Wetter", "Auto", "Hell", "Dunkel", "Diashow"]

# Every icon-carrying control in the header.
ICON_BUTTONS = [
    "#btn-mode-calendar",
    "#btn-mode-power",
    "#btn-mode-weather",
    "#btn-theme",
    "#btn-screensaver",
]


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


def test_every_control_uses_an_svg_icon_never_an_emoji(page: Page, server_url: str) -> None:
    """Regression guard for the tofu boxes on the real kiosk display: the
    icons must be drawn as SVG, so no font is needed to show them."""
    goto_calendar(page, server_url)
    for selector in ICON_BUTTONS:
        button = page.locator(selector)
        expect(button.locator(".btn-icon svg")).to_have_count(1)
        # The SVG actually draws something (shapes, not an empty element)...
        shapes = page.eval_on_selector(f"{selector} .btn-icon svg", "el => el.children.length")
        assert shapes >= 1, selector
        # ... and no text at all is left on the button.
        assert button.inner_text().strip() == "", (selector, button.inner_text())

    # The five icons are visibly different drawings, not the same shape.
    markups = page.eval_on_selector_all(
        ", ".join(f"{selector} .btn-icon svg" for selector in ICON_BUTTONS),
        "els => els.map((el) => el.innerHTML)",
    )
    assert len(set(markups)) == len(ICON_BUTTONS), markups


def test_icons_inherit_the_button_colour(page: Page, server_url: str) -> None:
    """`currentColor` is what makes the icon flip to the contrast colour
    inside the blue active button — no separate rule per state."""
    goto_calendar(page, server_url)
    paints = page.eval_on_selector_all(
        "#btn-mode-calendar .btn-icon svg > *",
        "els => els.map((el) => [el.getAttribute('stroke'), el.getAttribute('fill')])",
    )
    assert paints, "icon has no shapes"
    # Every shape is painted in currentColor — outlined ones via stroke,
    # solid ones via fill; the other channel is explicitly "none".
    for stroke, fill in paints:
        assert "currentColor" in (stroke, fill), (stroke, fill)


def test_screensaver_toggle_is_grey_when_off_and_coloured_when_on(
    page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)
    toggle = page.locator("#btn-screensaver")
    icon = "#btn-screensaver .btn-icon"

    def look() -> tuple[str, float]:
        return (
            page.eval_on_selector(icon, "el => getComputedStyle(el).color"),
            float(page.eval_on_selector(icon, "el => getComputedStyle(el).opacity")),
        )

    # Off: muted colour at reduced opacity — recognisably "inactive" from two
    # metres away (an SVG in currentColor has nothing to desaturate, so the
    # colour itself carries the state, not a grayscale filter).
    expect(toggle).to_have_attribute("aria-pressed", "false")
    off = look()
    assert off[1] < 1.0, off

    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")
    on = look()
    assert on != off, (off, on)
    assert on[1] == 1.0, on
    assert on[0] != off[0], (off, on)

    # And back off again.
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    assert look() == off


def test_theme_button_shows_a_distinct_symbol_per_state(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")

    def symbol() -> str:
        return page.eval_on_selector("#btn-theme .btn-icon svg", "el => el.innerHTML")

    # Auto, then light, then dark — three visibly different drawings, and the
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
