"""E2E tests for the photo slideshow screensaver.

The slideshow endpoints (/api/slideshow/next and .../image/{id}) are mocked
via Playwright route interception — the E2E server has no /media share. The
idle timeout and slide interval are shrunk to a few hundred ms via window
constants injected before the app's modules load, so the test never waits
the real three minutes.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

FIXTURE_PNG = Path(__file__).resolve().parent.parent / "fixtures" / "slideshow-photo.png"

# Short timings so the idle watcher fires quickly. The idle watcher polls
# once per second (IDLE_CHECK_INTERVAL_MS), so the timeout must clear that.
FAST_IDLE_MS = 1200
FAST_INTERVAL_MS = 400


def _mock_slideshow(page: Page, taken: dict | None = None, folders: list | None = None) -> None:
    png = FIXTURE_PNG.read_bytes()
    payload = {
        "id": 1,
        "name": "urlaub.jpg",
        "taken": taken,
        "folders": folders if folders is not None else [],
    }
    page.route(
        "**/api/slideshow/next",
        lambda route: route.fulfill(json=payload),
    )
    page.route(
        "**/api/slideshow/image/**",
        lambda route: route.fulfill(body=png, content_type="image/png"),
    )


def _inject_fast_timings(page: Page) -> None:
    page.add_init_script(
        f"window.SCREENSAVER_IDLE_MS = {FAST_IDLE_MS};"
        f" window.SLIDESHOW_INTERVAL_MS = {FAST_INTERVAL_MS};"
    )


def test_toggle_enables_screensaver_and_idle_starts_slideshow(
    page: Page, server_url: str
) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(
        page,
        taken={"year": 2019, "month": 8, "day": 16, "hour": 17, "minute": 30},
        folders=["Photos", "2019", "Urlaub"],
    )
    goto_calendar(page, server_url)

    # Enable the screensaver via the toggle (the photo icon in #mode-slot).
    toggle = page.locator("#btn-screensaver")
    expect(toggle).to_have_attribute("aria-pressed", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "true")

    # After the (shrunk) idle timeout, the full-screen slideshow appears
    # with the fixture image loaded.
    overlay = page.locator(".slideshow-overlay")
    expect(overlay).to_be_visible(timeout=5000)
    visible_layer = page.locator(".slideshow-layer-visible")
    expect(visible_layer).to_be_visible()
    expect(page.locator(".slideshow-caption")).to_have_text("urlaub.jpg")
    # Metadata badges: taken-at date top right, folder trail top left.
    expect(page.locator(".slideshow-taken")).to_have_text("16.08.2019 17:30")
    # The chevron separator is deliberate display text.
    expect(page.locator(".slideshow-folders")).to_have_text(
        "Photos › 2019 › Urlaub"  # noqa: RUF001
    )

    # Any touch/click ends the slideshow and returns to the calendar.
    page.mouse.click(960, 540)
    expect(overlay).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_slideshow_hides_badges_without_metadata(page: Page, server_url: str) -> None:
    # taken=null and no folders: both badges stay hidden — "wenn du nichts
    # auslesen kannst soll einfach nichts da stehen".
    _inject_fast_timings(page)
    _mock_slideshow(page, taken=None, folders=[])
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)
    expect(page.locator(".slideshow-caption")).to_have_text("urlaub.jpg")
    expect(page.locator(".slideshow-taken")).to_be_hidden()
    expect(page.locator(".slideshow-folders")).to_be_hidden()


def test_screensaver_off_by_default_no_slideshow(page: Page, server_url: str) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    goto_calendar(page, server_url)

    # Toggle stays off; even after well past the idle timeout, no slideshow.
    expect(page.locator("#btn-screensaver")).to_have_attribute("aria-pressed", "false")
    page.wait_for_timeout(FAST_IDLE_MS + 1500)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_screensaver_toggle_persists_across_reload(page: Page, server_url: str) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator("#btn-screensaver")).to_have_attribute("aria-pressed", "true")

    page.reload()
    expect(page.locator("#btn-screensaver")).to_have_attribute("aria-pressed", "true")
    # Still armed after reload: the slideshow starts again on idle.
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)
