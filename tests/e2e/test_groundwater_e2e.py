"""E2E tests for the groundwater view: map, markers, sparklines, detail.

Every groundwater endpoint AND the map tile proxy are mocked via Playwright
route interception — the E2E server has no scraped station list behind it,
and a test must never reach the GKD, the NID or OpenStreetMap for real.

The ``_e2e`` suffix is not decoration: pytest imports test modules by their
basename, and ``tests/test_groundwater.py`` already owns the plain one (the
same reason ``test_slideshow_e2e.py`` is spelled that way).
"""

import base64
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

# A 1x1 transparent PNG, served for every proxied base map tile.
TILE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

DAY_MS = 86_400_000
# A fixed recent timestamp; the view never computes staleness itself.
MEASURED_AT = 1_790_000_000_000


def station(
    number: str,
    *,
    name: str | None = None,
    lat: float = 48.1374,
    lon: float = 11.5755,
    tier: str = "upper",
    aquifer: str = "Quartär",
    level: float | None = 508.98,
    depth: float | None = 4.28,
    situation: str | None = "kein Niedrigwasser",
    situation_class: int | None = 0,
) -> dict:
    return {
        "number": number,
        "name": name if name is not None else f"Messstelle {number}",
        "lat": lat,
        "lon": lon,
        "aquifer": aquifer,
        "tier": tier,
        "level_m_nn": level,
        "depth_m": depth,
        "measured_at": MEASURED_AT,
        "situation": situation,
        "situation_class": situation_class,
        "stale": False,
    }


# A handful of stations spread over the radius, one per low-water class, plus
# the real-world case of two stations sharing a well head (Obermenzing T 3 F
# upper / T 3 T deeper aquifer, ~8 m apart in water level).
STATIONS = [
    station("16704", name="München KP 95", situation="kein Niedrigwasser", situation_class=0),
    station(
        "16705",
        name="Freising Nord",
        lat=48.35,
        lon=11.72,
        situation="niedrig",
        situation_class=1,
        level=440.12,
    ),
    station(
        "16706",
        name="Starnberg Süd",
        lat=47.95,
        lon=11.35,
        situation="sehr niedrig",
        situation_class=2,
        level=601.44,
    ),
    station(
        "16707",
        name="Dachau West",
        lat=48.26,
        lon=11.42,
        situation="neuer Niedrigstwert",
        situation_class=3,
        level=470.05,
    ),
    station(
        "16708",
        name="Erding Ost",
        lat=48.30,
        lon=11.90,
        situation=None,
        situation_class=None,
        level=455.30,
    ),
    station("100", name="Obermenzing T 3 F", lat=48.171, lon=11.453, level=520.0, tier="upper"),
    station("101", name="Obermenzing T 3 T", lat=48.171, lon=11.453, level=512.0, tier="deep"),
]

# "16708" deliberately has NO series: a station without history must render
# a marker without a curve and a German hint in its detail view.
SPARKLINES = {
    "16704": [508.5, 508.7, 508.6, 509.0, 508.9],
    "16705": [440.6, 440.4, 440.2, 440.12],
    "16706": [602.0, 601.8, 601.6, 601.44],
    "16707": [471.0, 470.6, 470.2, 470.05],
    "100": [519.5, 519.8, 520.0],
    "101": [512.4, 512.2, 512.0],
}


def _history(values: list[float], start: int = 1_780_000_000_000) -> dict:
    return {"points": [{"t": start + index * DAY_MS, "v": v} for index, v in enumerate(values)]}


def mock_groundwater(
    page: Page,
    *,
    stations: list | None = None,
    sparklines: dict | None = None,
    stations_status: int = 200,
) -> None:
    if stations_status != 200:
        page.route(
            "**/api/groundwater/stations",
            lambda route: route.fulfill(
                status=stations_status,
                json={"detail": "Der Grundwasserdienst ist nicht erreichbar."},
            ),
        )
    else:
        page.route(
            "**/api/groundwater/stations",
            lambda route: route.fulfill(
                json={"stations": STATIONS if stations is None else stations}
            ),
        )
    page.route(
        "**/api/groundwater/sparklines*",
        lambda route: route.fulfill(
            json={"days": 90, "series": SPARKLINES if sparklines is None else sparklines}
        ),
    )
    page.route("**/api/groundwater/history/**", _history_route)
    page.route(
        "**/api/weather/tile/**",
        lambda route: route.fulfill(body=TILE_PNG, content_type="image/png"),
    )


def _history_route(route) -> None:
    number = route.request.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    values = SPARKLINES.get(number)
    if values is None:
        route.fulfill(json={"number": number, "points": []})
        return
    route.fulfill(json={"number": number, **_history(values)})


def open_groundwater_view(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-mode-groundwater").click()
    expect(page.locator(".groundwater-view")).to_be_visible()


def test_switch_to_the_groundwater_view_and_back(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    expect(page.locator("#period-title")).to_have_text("Grundwasser")
    expect(page.locator(".groundwater-map-title")).to_have_text("Grundwasser München")
    # Calendar-only toolbar controls disappear, like in power and weather.
    expect(page.locator("#btn-month")).to_be_hidden()
    expect(page.locator("#btn-prev")).to_be_hidden()
    expect(page.locator("#legend")).to_be_hidden()

    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator(".groundwater-view")).to_be_hidden()
    expect(page.locator("#btn-month")).to_be_visible()


def test_the_mode_button_carries_an_svg_icon_not_an_emoji(page: Page, server_url: str) -> None:
    """The kiosk browser has no font for emoji beyond the BMP — an emoji
    button would be an unlabelled empty box (Etappe 38)."""
    mock_groundwater(page)
    goto_calendar(page, server_url)

    button = page.locator("#btn-mode-groundwater")
    expect(button).to_have_attribute("aria-label", "Grundwasser")
    expect(button).to_have_attribute("title", "Grundwasser")
    expect(button.locator("svg")).to_have_count(1)
    assert (button.text_content() or "").strip() == ""
    box = button.bounding_box()
    assert box is not None and box["width"] >= 44 and box["height"] >= 44, box


def test_the_map_is_tiled_and_every_station_gets_a_marker(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    tiles = page.locator(".groundwater-tiles .groundwater-tile")
    expect(tiles.first).to_be_attached()
    assert tiles.count() >= 4, tiles.count()
    # Relative URLs through our own proxy (ingress!), never a foreign host.
    expect(tiles.first).to_have_attribute("src", re.compile(r"^api/weather/tile/base/\d+/\d+/\d+$"))

    expect(page.locator(".groundwater-marker")).to_have_count(len(STATIONS))
    # The loading hint is gone once the map is up (it would cover it).
    expect(page.locator(".groundwater-hint")).to_be_hidden()


def test_the_markers_carry_real_svg_sparklines(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    markers = page.locator(".groundwater-markers")
    # One single svg holds all markers (165 separate ones would be a lot of
    # DOM for a view the kiosk redraws on every zoom step).
    expect(markers).to_have_count(1)
    assert markers.evaluate("el => el.tagName") == "svg"

    sparks = page.locator(".groundwater-marker polyline.groundwater-spark")
    # One per station that actually has a series — "16708" has none.
    expect(sparks).to_have_count(len(SPARKLINES))
    points = sparks.first.get_attribute("points") or ""
    assert re.fullmatch(r"(\d+(\.\d+)?,\d+(\.\d+)?\s?)+", points.strip() + " "), points
    # Drawn, not written: no text node anywhere in the marker layer.
    assert (markers.text_content() or "").strip() == ""


def test_each_sparkline_uses_its_own_scale(page: Page, server_url: str) -> None:
    """The absolute levels in the radius run from ~404 m to ~660 m while a
    station moves by centimetres — on one shared scale every curve would be
    a flat line. Two stations 80 m apart in height but with the same shape
    must therefore produce the same sparkline."""
    same_shape = [
        station("a", lat=48.10, lon=11.50, level=440.0),
        station("b", lat=48.20, lon=11.65, level=520.0),
    ]
    mock_groundwater(
        page,
        stations=same_shape,
        sparklines={"a": [440.0, 440.4, 440.2], "b": [520.0, 520.4, 520.2]},
    )
    open_groundwater_view(page, server_url)

    sparks = page.locator("polyline.groundwater-spark")
    expect(sparks).to_have_count(2)
    assert sparks.nth(0).get_attribute("points") == sparks.nth(1).get_attribute("points")
    # …and the curve is not a flat line.
    ys = [float(pair.split(",")[1]) for pair in (sparks.nth(0).get_attribute("points")).split()]
    assert max(ys) - min(ys) > 2, ys


def test_tapping_a_marker_opens_its_detail(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    page.locator('.groundwater-marker[data-number="16705"]').click()
    panel = page.locator("#groundwater-popover .groundwater-detail")
    expect(panel).to_be_visible()
    expect(panel.locator(".popover-title")).to_have_text("Freising Nord")
    expect(panel).to_contain_text("Quartär")
    expect(panel).to_contain_text("oberes Stockwerk")
    # The water table above sea level and the depth below ground are both
    # shown — as separate, unmistakably labelled figures.
    expect(panel).to_contain_text("440,12 m ü. NN")
    expect(panel).to_contain_text("4,28 m unter Gelände")
    expect(panel).to_contain_text("niedrig")

    # The curve is SVG and plots the level only; the depth is never on it.
    chart = panel.locator(".groundwater-detail-svg")
    expect(chart).to_be_visible()
    expect(chart.locator("path.groundwater-detail-line")).to_have_count(1)
    axis_labels = chart.locator("text").all_text_contents()
    assert any(label.startswith("440") for label in axis_labels), axis_labels
    assert not any(label.strip() in {"4,28", "4"} for label in axis_labels), axis_labels

    # Escape closes it again.
    page.keyboard.press("Escape")
    expect(page.locator("#groundwater-popover")).to_be_hidden()


def test_a_station_without_history_shows_a_german_hint(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    page.locator('.groundwater-marker[data-number="16708"]').click()
    panel = page.locator("#groundwater-popover .groundwater-detail")
    expect(panel.locator(".groundwater-detail-empty")).to_have_text(
        "Für diese Messstelle liegt noch kein Verlauf vor."
    )
    expect(panel.locator(".groundwater-detail-svg")).to_have_count(0)
    # It still says what it knows, including that it carries no rating.
    expect(panel).to_contain_text("keine Einstufung")


def test_two_stations_on_identical_coordinates_are_both_reachable(
    page: Page, server_url: str
) -> None:
    """Obermenzing T 3 F (upper) and T 3 T (deeper aquifer) share a well
    head. Their cards are stacked rather than drawn on top of each other,
    and each detail view links to the other as a second way in."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    upper = page.locator('.groundwater-marker[data-number="100"]')
    deeper = page.locator('.groundwater-marker[data-number="101"]')
    expect(upper).to_have_count(1)
    expect(deeper).to_have_count(1)

    upper_box = upper.locator("rect.groundwater-card").bounding_box()
    deeper_box = deeper.locator("rect.groundwater-card").bounding_box()
    assert upper_box is not None and deeper_box is not None
    # Stacked vertically, not overlapping.
    assert deeper_box["y"] + deeper_box["height"] <= upper_box["y"] + 1, (upper_box, deeper_box)
    # Same longitude — the marker never lies about where the well is.
    assert abs(upper_box["x"] - deeper_box["x"]) < 1, (upper_box, deeper_box)

    upper.click()
    panel = page.locator("#groundwater-popover .groundwater-detail")
    expect(panel.locator(".popover-title")).to_have_text("Obermenzing T 3 F")
    expect(panel).to_contain_text("520,00 m ü. NN")
    # The sibling is offered right there, so a mis-tap is recoverable.
    sibling = panel.locator(".groundwater-sibling-btn")
    expect(sibling).to_have_count(1)
    expect(sibling).to_have_text("Obermenzing T 3 T")
    sibling.click()
    expect(page.locator("#groundwater-popover .popover-title")).to_have_text(
        "Obermenzing T 3 T"
    )
    expect(page.locator("#groundwater-popover .groundwater-detail")).to_contain_text(
        "512,00 m ü. NN"
    )

    # Tapping the deeper one directly works too.
    page.keyboard.press("Escape")
    deeper.click()
    expect(page.locator("#groundwater-popover .popover-title")).to_have_text(
        "Obermenzing T 3 T"
    )


def test_the_period_buttons_reload_the_sparklines(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    requested: list[str] = []
    page.route(
        "**/api/groundwater/sparklines*",
        lambda route: (
            requested.append(route.request.url),
            route.fulfill(json={"days": 30, "series": SPARKLINES}),
        )[-1],
    )
    open_groundwater_view(page, server_url)
    expect(page.locator("polyline.groundwater-spark").first).to_be_attached()

    buttons = page.locator(".groundwater-period-btn")
    expect(buttons).to_have_count(3)
    expect(buttons.nth(1)).to_have_class(re.compile("groundwater-period-active"))
    buttons.nth(0).click()
    expect(buttons.nth(0)).to_have_class(re.compile("groundwater-period-active"))
    page.wait_for_function("() => document.querySelectorAll('.groundwater-spark').length > 0")
    assert any("days=30" in url for url in requested), requested


def test_the_zoom_buttons_reload_the_tiles_and_clamp(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    first = page.locator(".groundwater-tile").first
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/9/"))
    page.locator(".groundwater-zoom-in").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/10/"))
    # Already at the closest level.
    page.locator(".groundwater-zoom-in").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/10/"))
    page.locator(".groundwater-zoom-out").click()
    page.locator(".groundwater-zoom-out").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/8/"))
    # ...and not below the widest level either.
    page.locator(".groundwater-zoom-out").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/8/"))
    # The map is still fully tiled and the markers moved with it.
    assert page.locator(".groundwater-tile").count() >= 4
    expect(page.locator(".groundwater-marker").first).to_be_attached()


def test_the_legend_names_every_low_water_class(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    labels = page.locator(".groundwater-legend-label").all_text_contents()
    assert labels == [
        "kein Niedrigwasser",
        "niedrig",
        "sehr niedrig",
        "neuer Niedrigstwert",
        "keine Einstufung",
    ], labels
    expect(page.locator(".groundwater-count")).to_contain_text("Messstellen")


def test_the_attribution_names_the_source(page: Page, server_url: str) -> None:
    """CC BY 4.0: naming the Bayerisches Landesamt für Umwelt is a licence
    condition, not decoration."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    attribution = page.locator(".groundwater-attribution")
    expect(attribution).to_be_visible()
    expect(attribution).to_contain_text("Bayerisches Landesamt für Umwelt")
    expect(attribution).to_contain_text("www.lfu.bayern.de")
    expect(attribution).to_contain_text("Niedrigwasser")
    expect(attribution).to_contain_text("OpenStreetMap")


def test_the_mode_survives_a_reload(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    page.reload()
    expect(page.locator(".groundwater-view")).to_be_visible()
    expect(page.locator("#period-title")).to_have_text("Grundwasser")
    expect(page.locator("#btn-mode-groundwater")).to_have_attribute("aria-pressed", "true")


def test_the_view_fits_the_kiosk_without_scrolling(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    open_groundwater_view(page, server_url)
    expect(page.locator(".groundwater-marker").first).to_be_attached()

    overflow = page.eval_on_selector("#groundwater", "el => el.scrollHeight - el.clientHeight")
    assert overflow <= 1, overflow

    # Map left, controls right, and the map is (near enough) square.
    map_box = page.locator(".groundwater-map").bounding_box()
    side_box = page.locator(".groundwater-side").bounding_box()
    assert map_box is not None and side_box is not None
    assert map_box["x"] + map_box["width"] <= side_box["x"] + 1, (map_box, side_box)
    assert abs(map_box["width"] - map_box["height"]) <= 2, map_box
    assert map_box["y"] + map_box["height"] <= 1080 + 1, map_box


def test_the_narrow_panel_stays_usable(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    page.set_viewport_size({"width": 390, "height": 844})
    open_groundwater_view(page, server_url)
    expect(page.locator(".groundwater-marker").first).to_be_attached()

    # Stacked, and nothing sticks out sideways.
    map_box = page.locator(".groundwater-map").bounding_box()
    side_box = page.locator(".groundwater-side").bounding_box()
    assert map_box is not None and side_box is not None
    assert side_box["y"] >= map_box["y"] + map_box["height"] - 1, (map_box, side_box)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, overflow


def test_an_unreachable_station_list_shows_a_german_message(page: Page, server_url: str) -> None:
    mock_groundwater(page, stations_status=502)
    open_groundwater_view(page, server_url)

    hint = page.locator(".groundwater-hint")
    expect(hint).to_be_visible()
    expect(hint).to_have_text("Der Grundwasserdienst ist nicht erreichbar.")
    # The rest of the view is still there — a failing source never takes the
    # whole page with it.
    expect(page.locator(".groundwater-attribution")).to_be_visible()
    expect(page.locator(".groundwater-legend-item")).to_have_count(5)


def test_failing_sparklines_still_leave_a_usable_map(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    page.route(
        "**/api/groundwater/sparklines*",
        lambda route: route.fulfill(status=502, json={"detail": "Nicht erreichbar."}),
    )
    open_groundwater_view(page, server_url)

    # Markers and dots are drawn, they just carry no curve.
    expect(page.locator(".groundwater-marker")).to_have_count(len(STATIONS))
    expect(page.locator("polyline.groundwater-spark")).to_have_count(0)
    expect(page.locator(".groundwater-hint")).to_be_hidden()
    # And the detail view still works.
    page.locator('.groundwater-marker[data-number="16704"]').click()
    expect(page.locator("#groundwater-popover .popover-title")).to_have_text("München KP 95")


def test_leaving_the_view_stops_its_polling(page: Page, server_url: str) -> None:
    """Only the active view may keep timers running (MODES in main.js)."""
    mock_groundwater(page)
    calls: list[str] = []
    page.route(
        "**/api/groundwater/stations",
        lambda route: (
            calls.append(route.request.url),
            route.fulfill(json={"stations": STATIONS}),
        )[-1],
    )
    open_groundwater_view(page, server_url)
    expect(page.locator(".groundwater-marker").first).to_be_attached()
    before = len(calls)

    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    # The popover is closed with the view, and nothing is fetched any more.
    expect(page.locator("#groundwater-popover")).to_be_hidden()
    page.wait_for_timeout(600)
    assert len(calls) == before, calls
