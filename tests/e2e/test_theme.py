"""E2E tests for the dark-theme support (Etappe 19).

The header toggle cycles auto -> light -> dark, sets documentElement's
data-theme accordingly and visibly changes the page background. The choice
persists per device (localStorage) across reloads, and "auto" follows the
browser's prefers-color-scheme (emulated here).
"""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e


def _body_bg(page: Page) -> str:
    return page.eval_on_selector(
        "body", "el => getComputedStyle(el).backgroundColor"
    )


def _data_theme(page: Page) -> str | None:
    return page.evaluate("() => document.documentElement.dataset.theme ?? null")


def test_toggle_cycles_theme_and_changes_background(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")

    # Default: auto -> no override attribute, light background (test browser
    # defaults to light color scheme).
    assert _data_theme(page) is None
    light_bg = _body_bg(page)

    # First tap -> light (explicit): still a light background.
    button.click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")

    # Second tap -> dark: the background must actually change.
    button.click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    dark_bg = _body_bg(page)
    assert dark_bg != light_bg, "dark theme must change the page background"

    # Third tap -> back to auto: override removed.
    button.click()
    assert _data_theme(page) is None


def test_theme_choice_persists_across_reload(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")
    button.click()  # auto -> light
    button.click()  # light -> dark
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    dark_bg = _body_bg(page)

    page.reload()
    expect(page.locator("#calendar .month-view, #calendar .week-view")).to_be_visible()
    # The stored dark theme is applied again before the first paint.
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert _body_bg(page) == dark_bg


def test_auto_follows_prefers_color_scheme_dark(page: Page, server_url: str) -> None:
    # In "auto" (the default, no override) the page follows the system theme.
    page.emulate_media(color_scheme="light")
    goto_calendar(page, server_url)
    assert _data_theme(page) is None
    light_bg = _body_bg(page)

    page.emulate_media(color_scheme="dark")
    dark_bg = _body_bg(page)
    assert dark_bg != light_bg, "auto theme must follow prefers-color-scheme: dark"


def test_manual_light_wins_over_dark_system(page: Page, server_url: str) -> None:
    # A manual "light" choice must override a dark system preference.
    page.emulate_media(color_scheme="dark")
    goto_calendar(page, server_url)
    button = page.locator("#btn-theme")
    button.click()  # auto -> light
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    # Light background despite the dark system setting.
    light_bg = _body_bg(page)

    button.click()  # light -> dark
    dark_bg = _body_bg(page)
    assert dark_bg != light_bg
