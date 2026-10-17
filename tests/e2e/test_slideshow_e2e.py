"""E2E tests for the photo slideshow screensaver.

The slideshow endpoints (/api/slideshow/next and .../image/{id}) are mocked
via Playwright route interception — the E2E server has no /media share. The
idle timeout and slide interval are shrunk to a few hundred ms via window
constants injected before the app's modules load, so the test never waits
the real three minutes.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

FIXTURE_PNG = Path(__file__).resolve().parent.parent / "fixtures" / "slideshow-photo.png"

# Short timings so the idle watcher fires quickly. The idle watcher polls
# once per second (IDLE_CHECK_INTERVAL_MS), so the timeout must clear that.
FAST_IDLE_MS = 1200
FAST_INTERVAL_MS = 400
# The operating icons' window (real value: 5 s) — long enough to click an
# icon in, short enough that waiting it out does not slow the suite down.
FAST_CONTROLS_MS = 1500


def _mock_slideshow(
    page: Page,
    taken: dict | None = None,
    folders: list | None = None,
    rotation: int = 0,
) -> None:
    png = FIXTURE_PNG.read_bytes()
    payload = {
        "id": 1,
        "name": "urlaub.jpg",
        "kind": "image",
        "taken": taken,
        "folders": folders if folders is not None else [],
        "rotation": rotation,
    }
    page.route(
        "**/api/slideshow/next",
        lambda route: route.fulfill(json=payload),
    )
    page.route(
        "**/api/slideshow/image/**",
        lambda route: route.fulfill(body=png, content_type="image/png"),
    )


def _inject_fast_timings(
    page: Page,
    controls_ms: int = FAST_CONTROLS_MS,
    interval_ms: int = FAST_INTERVAL_MS,
) -> None:
    page.add_init_script(
        f"window.SCREENSAVER_IDLE_MS = {FAST_IDLE_MS};"
        f" window.SLIDESHOW_INTERVAL_MS = {interval_ms};"
        f" window.SLIDESHOW_CONTROLS_MS = {controls_ms};"
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

    # Since Etappe 49 it takes two touches: the first brings up the three
    # operating icons, the second (beside them) ends the slideshow and
    # returns to the calendar.
    page.mouse.click(960, 540)
    expect(page.locator(".slideshow-controls")).to_be_visible()
    expect(overlay).to_have_count(1)
    page.mouse.click(960, 540)
    expect(overlay).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_slideshow_overlay_covers_the_whole_header(page: Page, server_url: str) -> None:
    """During the slideshow no header control may show through or be clickable
    over the photo (Etappe 37: Roland saw a calendar button over the images).
    The full-screen overlay must sit above every toolbar element."""
    _inject_fast_timings(page)
    _mock_slideshow(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    overlay = page.locator(".slideshow-overlay")
    expect(overlay).to_be_visible(timeout=5000)

    # At each header control's centre, the top-most element must belong to the
    # slideshow overlay — never the toolbar button underneath it.
    for selector in ["#btn-mode-calendar", "#btn-mode-power", "#btn-screensaver", "#btn-today"]:
        covered = page.eval_on_selector(
            selector,
            """el => {
                const r = el.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    r.left + r.width / 2, r.top + r.height / 2);
                return hit !== null && hit.closest('.slideshow-overlay') !== null;
            }""",
        )
        assert covered, selector


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

    # Touching still ends the slideshow and returns to the calendar — since
    # Etappe 49 on the second touch (the first opens the operating icons).
    page.mouse.click(960, 540)
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


# -- operating icons at the kiosk (Etappe 49) --------------------------------
#
# One touch brings up three big corner icons (mark for deletion, turn left,
# turn right) and keeps the picture on screen; a second touch BESIDE them
# ends the slideshow exactly as before. Visibility and the "a tap ends it"
# window are deliberately the same five seconds, so there is never a state in
# which the icons are up but a tap next to them does nothing.

DELETE_BUTTON = "#slideshow-mark-delete"
CCW_BUTTON = "#slideshow-rotate-ccw"
CW_BUTTON = "#slideshow-rotate-cw"
CONTROL_BUTTONS = (DELETE_BUTTON, CCW_BUTTON, CW_BUTTON)


def _mock_sequence(page: Page, names: list[str]) -> None:
    """Serve a different item per request so an advance is visible."""
    png = FIXTURE_PNG.read_bytes()
    state = {"served": 0}

    def next_item(route) -> None:
        index = min(state["served"], len(names) - 1)
        state["served"] += 1
        route.fulfill(
            json={
                "id": index + 1,
                "name": names[index],
                "kind": "image",
                "taken": None,
                "folders": [],
                "rotation": 0,
            }
        )

    page.route("**/api/slideshow/next", next_item)
    page.route(
        "**/api/slideshow/image/**",
        lambda route: route.fulfill(body=png, content_type="image/png"),
    )


def _centre(page: Page) -> tuple[float, float]:
    viewport = page.viewport_size
    assert viewport is not None
    return viewport["width"] / 2, viewport["height"] / 2


def _start_slideshow(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-screensaver").click()
    expect(page.locator(".slideshow-overlay")).to_be_visible(timeout=5000)
    expect(page.locator(".slideshow-layer-visible")).to_be_visible()


def _tap_beside_the_icons(page: Page) -> None:
    x, y = _centre(page)
    page.mouse.click(x, y)


def test_first_touch_opens_the_icons_and_keeps_the_slideshow(
    page: Page, server_url: str
) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-controls")).to_be_visible()
    # The slideshow is still up - the first touch only arms the controls.
    expect(page.locator(".slideshow-overlay")).to_have_count(1)

    for selector in CONTROL_BUTTONS:
        button = page.locator(selector)
        expect(button).to_be_visible()
        box = button.bounding_box()
        assert box is not None, selector
        # Tapped in the dark from two metres: far above the usual 48px.
        assert box["width"] >= 64 and box["height"] >= 64, (selector, box)
        # Drawn as SVG, never an emoji (the kiosk has no font for those).
        assert button.locator("svg").count() == 1, selector
        assert button.inner_text().strip() == "", selector


def test_second_touch_beside_the_icons_ends_the_slideshow(
    page: Page, server_url: str
) -> None:
    _inject_fast_timings(page)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-controls")).to_be_visible()
    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_tapping_an_icon_keeps_the_slideshow_and_restarts_the_window(
    page: Page, server_url: str
) -> None:
    """So two turns in a row are possible without the bar running away."""
    _inject_fast_timings(page, controls_ms=1500)
    _mock_slideshow(page)
    page.route(
        "**/api/slideshow/rotate/**",
        lambda route: route.fulfill(json={"id": 1, "rotation": 90}),
    )
    _start_slideshow(page, server_url)

    _tap_beside_the_icons(page)
    controls = page.locator(".slideshow-controls")
    expect(controls).to_be_visible()
    page.wait_for_timeout(1000)  # most of the first window is gone
    page.locator(CW_BUTTON).click()
    # The tap on an icon must NOT end the slideshow...
    expect(page.locator(".slideshow-overlay")).to_have_count(1)
    # ...and it restarts the window: past the first one, still visible.
    page.wait_for_timeout(1000)
    expect(controls).to_be_visible()


def test_the_icons_vanish_after_the_window_and_return_on_the_next_touch(
    page: Page, server_url: str
) -> None:
    _inject_fast_timings(page, controls_ms=800)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    controls = page.locator(".slideshow-controls")
    _tap_beside_the_icons(page)
    expect(controls).to_be_visible()
    expect(controls).to_be_hidden(timeout=5000)
    # The slideshow itself kept running.
    expect(page.locator(".slideshow-overlay")).to_have_count(1)
    # The next touch starts from the beginning again.
    _tap_beside_the_icons(page)
    expect(controls).to_be_visible()


@pytest.fixture()
def touch_page(browser: Browser) -> Iterator[Page]:
    """A page that reports itself as a touchscreen, like the Waveshare kiosk.

    Every other test here drives the slideshow with the mouse, which emits a
    different event sequence than a finger — so the way Roland actually
    operates the display was the one path with no coverage at all.
    """
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, has_touch=True)
    page = context.new_page()
    yield page
    context.close()


def test_the_whole_sequence_works_with_a_finger(touch_page: Page, server_url: str) -> None:
    page = touch_page
    _inject_fast_timings(page, controls_ms=1500)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    controls = page.locator(".slideshow-controls")
    overlay = page.locator(".slideshow-overlay")
    x, y = _centre(page)

    # First tap: icons up, slideshow stays.
    page.touchscreen.tap(x, y)
    expect(controls).to_be_visible()
    expect(overlay).to_have_count(1)

    # Wait the window out, then tap again — the icons must come BACK, not
    # drop to the calendar.
    expect(controls).to_be_hidden(timeout=6000)
    expect(overlay).to_have_count(1)
    page.touchscreen.tap(x, y)
    expect(controls).to_be_visible()
    expect(overlay).to_have_count(1)

    # Only a tap while they are up ends the slideshow.
    page.touchscreen.tap(x, y)
    expect(overlay).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_icons_that_do_not_stay_up_hand_the_tap_back_to_the_calendar(
    page: Page, server_url: str
) -> None:
    """The kiosk must never swallow a touch.

    Roland, 17.10.2026: "aktuell kann ich tapen und es kommt weder icon noch
    kalender" — the worst possible outcome at a display with no keyboard and
    no way out. Whatever keeps the icons off the screen, the second attempt
    in a row to open them gives up and lets the slideshow end: landing in the
    calendar always beats tapping a display that does nothing.

    Here the icons are taken down behind the module's back, so its own hide
    path (which clears the suspicion) never runs and the next tap lands on
    the "open" branch a second time.
    """
    _inject_fast_timings(page, controls_ms=30000)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    controls = page.locator(".slideshow-controls")
    _tap_beside_the_icons(page)
    expect(controls).to_be_visible()

    page.eval_on_selector(".slideshow-controls", "node => { node.hidden = true; }")
    expect(controls).to_be_hidden()

    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_a_throw_inside_the_slideshow_still_returns_to_the_calendar(
    page: Page, server_url: str
) -> None:
    """The same guarantee from the other side: if the slideshow's own
    handling raises, the exception must not skip the way out. Simulated by
    making the controls element refuse to be unhidden."""
    _inject_fast_timings(page, controls_ms=30000)
    _mock_slideshow(page)
    _start_slideshow(page, server_url)

    page.eval_on_selector(
        ".slideshow-controls",
        """node => Object.defineProperty(node, "hidden", {
            get: () => true,
            set: () => {
                throw new Error("boom");
            },
        })""",
    )

    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-overlay")).to_have_count(0)
    expect(page.locator("#calendar .month-view")).to_be_visible()


def test_the_picture_does_not_advance_while_the_icons_are_up(
    page: Page, server_url: str
) -> None:
    """Otherwise the item changes mid-tap and the wrong photo is turned or
    marked for deletion."""
    # A visible but unhurried cadence: the picture would change twice over
    # during the hold below if the hold did not work.
    _inject_fast_timings(page, controls_ms=4000, interval_ms=1200)
    _mock_sequence(page, [f"bild-{index}.jpg" for index in range(1, 11)])
    _start_slideshow(page, server_url)

    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-controls")).to_be_visible()
    shown = page.locator(".slideshow-caption").inner_text()
    page.wait_for_timeout(2600)
    assert page.locator(".slideshow-caption").inner_text() == shown

    # Once the window is over the rotation carries on by itself.
    expect(page.locator(".slideshow-caption")).not_to_have_text(shown, timeout=10000)


def test_turning_a_photo_rotates_the_layer_and_swaps_its_edges(
    page: Page, server_url: str
) -> None:
    """The backend answers with the new ABSOLUTE angle; the view applies it
    without reloading the picture. A quarter turn also swaps the layer's
    edges - object-fit: contain fits the picture before the rotation."""
    _inject_fast_timings(page, controls_ms=4000)
    _mock_slideshow(page)
    angle = {"value": 0}

    def rotate(route) -> None:
        body = json.loads(route.request.post_data or "{}")
        step = 90 if body.get("direction") == "cw" else -90
        angle["value"] = (angle["value"] + step) % 360
        route.fulfill(json={"id": 1, "rotation": angle["value"]})

    page.route("**/api/slideshow/rotate/**", rotate)
    _start_slideshow(page, server_url)

    _tap_beside_the_icons(page)
    page.locator(CW_BUTTON).click()
    page.wait_for_function(
        "() => { const l = document.querySelector('.slideshow-layer-visible');"
        " return l !== null && l.style.transform.includes('rotate(90deg)'); }"
    )
    geometry = page.evaluate(
        """() => {
            const layer = document.querySelector('.slideshow-layer-visible');
            const overlay = document.querySelector('.slideshow-overlay');
            return {
                width: layer.style.width,
                height: layer.style.height,
                transform: getComputedStyle(layer).transform,
                frameWidth: overlay.clientWidth,
                frameHeight: overlay.clientHeight,
            };
        }"""
    )
    assert geometry["width"] == f"{geometry['frameHeight']}px", geometry
    assert geometry["height"] == f"{geometry['frameWidth']}px", geometry
    # translate(-50%, -50%) rotate(90deg) == matrix(0, 1, -1, 0, tx, ty)
    assert geometry["transform"].startswith("matrix(0, 1, -1, 0"), geometry

    # Turning back the other way returns it upright, filling the frame again.
    page.locator(CCW_BUTTON).click()
    page.wait_for_function(
        "() => { const l = document.querySelector('.slideshow-layer-visible');"
        " return l !== null && l.style.transform.includes('rotate(0deg)'); }"
    )
    upright = page.evaluate(
        "() => { const l = document.querySelector('.slideshow-layer-visible');"
        " return {width: l.style.width, height: l.style.height}; }"
    )
    assert upright == {"width": "100%", "height": "100%"}, upright


def test_marking_for_deletion_moves_on_to_the_next_item(
    page: Page, server_url: str
) -> None:
    """The backend stops handing a marked photo out, so leaving it on screen
    would be contradictory - it is replaced right away, with a short note."""
    # A long slide interval: the only thing that may advance here is the tap.
    _inject_fast_timings(page, controls_ms=4000, interval_ms=30000)
    _mock_sequence(page, ["eins.jpg", "zwei.jpg"])
    marked: list[str] = []

    def mark(route) -> None:
        marked.append(route.request.url)
        route.fulfill(
            json={
                "id": 1,
                "delete_requested": True,
                "marked_at": "2026-10-10T08:00:00+00:00",
            }
        )

    page.route("**/api/slideshow/mark-delete/**", mark)
    _start_slideshow(page, server_url)
    expect(page.locator(".slideshow-caption")).to_have_text("eins.jpg")

    _tap_beside_the_icons(page)
    page.locator(DELETE_BUTTON).click()
    expect(page.locator(".slideshow-notice")).to_contain_text("vorgemerkt")
    expect(page.locator(".slideshow-caption")).to_have_text("zwei.jpg", timeout=10000)
    assert marked and marked[0].endswith("/api/slideshow/mark-delete/1"), marked
    # The slideshow is still running - marking is not "end the screensaver".
    expect(page.locator(".slideshow-overlay")).to_have_count(1)


def test_the_icons_do_not_cover_the_vertical_edge_overlays(
    page: Page, server_url: str
) -> None:
    """The filename/date/folder badges (Etappe 34) sit on the screen edges;
    the icons are inset far enough to leave them readable."""
    _inject_fast_timings(page, controls_ms=4000)
    _mock_slideshow(
        page,
        taken={"year": 2019, "month": 8, "day": 16, "hour": 17, "minute": 30},
        folders=["Photos", "2019", "Urlaub"],
    )
    page.set_viewport_size({"width": 1920, "height": 1080})
    _start_slideshow(page, server_url)
    _tap_beside_the_icons(page)
    expect(page.locator(".slideshow-controls")).to_be_visible()

    def box(selector: str) -> dict:
        found = page.locator(selector).bounding_box()
        assert found is not None, selector
        return found

    def overlaps(a: dict, b: dict) -> bool:
        return (
            a["x"] < b["x"] + b["width"]
            and b["x"] < a["x"] + a["width"]
            and a["y"] < b["y"] + b["height"]
            and b["y"] < a["y"] + a["height"]
        )

    for badge in (".slideshow-folders", ".slideshow-taken", ".slideshow-caption"):
        for control in CONTROL_BUTTONS:
            assert not overlaps(box(badge), box(control)), (badge, control)
