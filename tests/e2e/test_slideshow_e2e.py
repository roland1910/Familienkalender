"""E2E tests for the photo slideshow screensaver.

The slideshow endpoints (/api/slideshow/next and .../image/{id}) are mocked
via Playwright route interception — the E2E server has no /media share. The
idle timeout and slide interval are shrunk to a few hundred ms via window
constants injected before the app's modules load, so the test never waits
the real three minutes.
"""

from collections.abc import Iterator
from pathlib import Path

import httpx
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
        "kind": "image",
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
    # The photo layer is a real <img> pointing at the image endpoint.
    assert visible_layer.evaluate("node => node.tagName") == "IMG"
    assert "api/slideshow/image/1" in visible_layer.get_attribute("src")
    expect(page.locator(".slideshow-caption")).to_have_text("urlaub.jpg")
    # Metadata badges: taken-at date and folder trail (vertical, on the
    # screen edges — the exact placement is pinned further down).
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


def test_photo_layers_are_images_honouring_exif_orientation(
    page: Page, server_url: str
) -> None:
    """Etappe 32 regression: the layers were background-image divs, which
    ignore the EXIF orientation tag — portrait photos showed up rotated by
    90° on the kiosk. They must be <img> elements with the orientation
    explicitly taken from the image (and no background image at all)."""
    _inject_fast_timings(page)
    _mock_slideshow(page, taken={"year": 2019}, folders=["Photos", "2019"])
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)

    layers = page.locator(".slideshow-layer")
    expect(layers).to_have_count(2)
    for index in range(2):
        layer = layers.nth(index)
        assert layer.evaluate("node => node.tagName") == "IMG"
        styles = layer.evaluate(
            "node => { const s = getComputedStyle(node);"
            " return {orientation: s.imageOrientation, fit: s.objectFit,"
            " background: s.backgroundImage}; }"
        )
        assert styles["orientation"] == "from-image", styles
        assert styles["fit"] == "contain", styles
        assert styles["background"] == "none", styles

    # The metadata badges and the caption still stack above the photo layers:
    # higher z-index within the same overlay stacking context.
    layer_z = int(layers.first.evaluate("node => getComputedStyle(node).zIndex"))
    for selector in (".slideshow-taken", ".slideshow-folders", ".slideshow-caption"):
        badge = page.locator(selector)
        expect(badge).to_be_visible()
        badge_z = int(badge.evaluate("node => getComputedStyle(node).zIndex"))
        assert badge_z > layer_z, f"{selector} liegt nicht über den Foto-Ebenen"


def test_overlay_texts_run_vertically_along_the_screen_edges(
    page: Page, server_url: str
) -> None:
    """Etappe 34: the three overlays are rotated by 90° and sit on the screen
    edges — folder trail on the left reading bottom-to-top and ending at the
    top, taken-at date on the right starting in the top corner, filename on
    the right ending in the bottom corner. Rotation is done with
    `writing-mode` (not a bare transform) so wrapping/ellipsis still work;
    the bottom-to-top direction needs the extra 180° turn because WebKit —
    which the kiosk runs — does not support `sideways-lr`."""
    _inject_fast_timings(page)
    _mock_slideshow(
        page,
        taken={"year": 2019, "month": 8, "day": 16, "hour": 17, "minute": 30},
        folders=["Photos", "2019", "Urlaub"],
    )
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)

    viewport = page.viewport_size
    assert viewport is not None
    width, height = viewport["width"], viewport["height"]
    margin = 80  # generous: the exact inset is a styling detail

    def box_and_style(selector: str) -> tuple[dict, dict]:
        locator = page.locator(selector)
        expect(locator).to_be_visible()
        box = locator.bounding_box()
        assert box is not None, selector
        style = locator.evaluate(
            "node => { const s = getComputedStyle(node);"
            " return {writingMode: s.writingMode, transform: s.transform,"
            " events: s.pointerEvents}; }"
        )
        return box, style

    for selector in (".slideshow-folders", ".slideshow-taken", ".slideshow-caption"):
        box, style = box_and_style(selector)
        assert style["writingMode"] == "vertical-rl", (selector, style)
        # Rotated text: the box is taller than it is wide.
        assert box["height"] > box["width"], (selector, box)
        assert style["events"] == "none", (selector, style)

    folders, folders_style = box_and_style(".slideshow-folders")
    # Left edge, anchored at the top; the extra half turn makes it readable
    # bottom-to-top (matrix(-1, 0, 0, -1, 0, 0) == rotate(180deg)).
    assert folders["x"] < margin, folders
    assert folders["y"] < margin, folders
    assert "matrix(-1, 0, 0, -1" in folders_style["transform"], folders_style

    taken, _ = box_and_style(".slideshow-taken")
    # Right edge, starting in the top corner.
    assert taken["x"] + taken["width"] > width - margin, taken
    assert taken["y"] < margin, taken

    caption, _ = box_and_style(".slideshow-caption")
    # Right edge, ending in the bottom corner.
    assert caption["x"] + caption["width"] > width - margin, caption
    assert caption["y"] + caption["height"] > height - margin, caption

    # The two right-edge overlays never overlap vertically.
    assert taken["y"] + taken["height"] < caption["y"], (taken, caption)


def test_screensaver_off_by_default_no_slideshow(page: Page, server_url: str) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    goto_calendar(page, server_url)

    # Toggle stays off; even after well past the idle timeout, no slideshow.
    expect(page.locator("#btn-screensaver")).to_have_attribute("aria-pressed", "false")
    page.wait_for_timeout(FAST_IDLE_MS + 1500)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def _put_screensaver_default(server_url: str, value: str) -> None:
    response = httpx.put(
        f"{server_url}/api/admin/settings",
        json={"evening_boundary": "17:00", "screensaver_default": value},
        timeout=10.0,
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def screensaver_default_on(server_url: str) -> Iterator[None]:
    # Flipped via the admin API (localhost = admin in this suite) and reset
    # afterwards so the other E2E files keep starting with the saver off.
    _put_screensaver_default(server_url, "on")
    yield
    _put_screensaver_default(server_url, "off")


def test_server_default_on_starts_slideshow_without_toggle_tap(
    screensaver_default_on: None, page: Page, server_url: str
) -> None:
    # Fresh context (empty localStorage — the kiosk after a restart) and no
    # tap on the photo toggle: the server default arms the screensaver.
    _inject_fast_timings(page)
    _mock_slideshow(page)
    goto_calendar(page, server_url)
    expect(page.locator("#btn-screensaver")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)

    # A touch still ends the slideshow and returns to the calendar.
    page.mouse.click(960, 540)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_device_off_choice_wins_over_server_default_on(
    screensaver_default_on: None, page: Page, server_url: str
) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    goto_calendar(page, server_url)
    # Deliberate device choice: switch the armed saver OFF (persisted).
    toggle = page.locator("#btn-screensaver")
    expect(toggle).to_have_attribute("aria-pressed", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-pressed", "false")

    # The server default (on) must NOT re-arm it on reload.
    page.reload()
    expect(toggle).to_have_attribute("aria-pressed", "false")
    page.wait_for_timeout(FAST_IDLE_MS + 1500)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)


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


# -- videos (Etappe 33) ------------------------------------------------------
#
# Real playback is deliberately NOT exercised here: it would need a decodable
# fixture clip and a codec the CI browser supports, which is exactly the kind
# of flake an E2E suite should not carry. What IS pinned is the part the
# kiosk depends on — that a video item gets a proper <video> layer (muted,
# playsinline, no controls), and that an undecodable clip never leaves the
# screen standing still but moves on to the next item.


def _mock_video_slideshow(page: Page) -> None:
    """Every item is a video whose bytes are not a decodable clip."""
    page.route(
        "**/api/slideshow/next",
        lambda route: route.fulfill(
            json={
                "id": 7,
                "name": "urlaub.mp4",
                "kind": "video",
                "taken": {"year": 2019, "month": 8, "day": 16},
                "folders": ["Photos", "2019"],
            }
        ),
    )
    page.route(
        "**/api/slideshow/image/**",
        lambda route: route.fulfill(body=b"not-a-real-video", content_type="video/mp4"),
    )


def test_video_item_gets_a_muted_inline_video_layer(page: Page, server_url: str) -> None:
    _inject_fast_timings(page)
    _mock_video_slideshow(page)
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)

    video = page.locator("video.slideshow-layer")
    expect(video).to_have_count(1, timeout=5000)
    props = video.evaluate(
        "node => ({muted: node.muted, controls: node.controls,"
        " inline: node.hasAttribute('playsinline'),"
        " fit: getComputedStyle(node).objectFit})"
    )
    assert props["muted"] is True, props
    assert props["controls"] is False, props
    assert props["inline"] is True, props
    # Same letterboxing as the photos — no cropping, no stretching.
    assert props["fit"] == "contain", props


def test_undecodable_video_moves_on_to_the_next_item(page: Page, server_url: str) -> None:
    """A codec the kiosk cannot decode must never freeze the slideshow: the
    error path fetches the next item instead of sitting on a black screen."""
    _inject_fast_timings(page)
    png = FIXTURE_PNG.read_bytes()
    served = {"count": 0}

    def next_item(route) -> None:
        served["count"] += 1
        if served["count"] == 1:
            route.fulfill(
                json={
                    "id": 7,
                    "name": "kaputt.mp4",
                    "kind": "video",
                    "taken": None,
                    "folders": [],
                }
            )
        else:
            route.fulfill(
                json={
                    "id": 8,
                    "name": "danach.jpg",
                    "kind": "image",
                    "taken": None,
                    "folders": [],
                }
            )

    page.route("**/api/slideshow/next", next_item)
    page.route(
        "**/api/slideshow/image/7",
        lambda route: route.fulfill(body=b"not-a-real-video", content_type="video/mp4"),
    )
    page.route(
        "**/api/slideshow/image/8",
        lambda route: route.fulfill(body=png, content_type="image/png"),
    )

    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)
    # The broken clip is skipped and the following photo is shown instead.
    expect(page.locator(".slideshow-caption")).to_have_text("danach.jpg", timeout=10000)
    visible = page.locator(".slideshow-layer-visible")
    assert visible.evaluate("node => node.tagName") == "IMG"
