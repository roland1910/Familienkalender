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

# The four extremes of the bounding box the live service really returns
# (178 stations, measured 2026-09-22): ~47 km north/south and ~49/46 km
# east/west of Munich. They are what "die äussersten messpunkte" means.
RIM_STATIONS = [
    station("rim-n", name="Nordrand", lat=48.5115, lon=11.5755),
    station("rim-s", name="Südrand", lat=47.7124, lon=11.5755),
    station("rim-o", name="Ostrand", lat=48.1374, lon=12.2367),
    station("rim-w", name="Westrand", lat=48.1374, lon=10.9527),
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


def zoom_in(page: Page, steps: int = 2) -> None:
    """Zoom to the closest level (the buttons clamp, so two taps suffice)."""
    for _ in range(steps):
        page.locator(".groundwater-zoom-in").click()


def test_the_markers_carry_real_svg_sparklines(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)
    zoom_in(page)

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
    zoom_in(page)

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
    zoom_in(page)

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


def test_the_overview_shows_dots_only_and_the_curves_come_with_the_zoom(
    page: Page, server_url: str
) -> None:
    """The density rule (Etappe 47): around Munich the stations stand so
    close that their cards overlapped into an unreadable block. At the
    default zoom the map is a plain traffic light; the curves appear once
    Roland zooms into a corner of it."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    # Every station is still on the map — only its card is gone.
    expect(page.locator(".groundwater-marker")).to_have_count(len(STATIONS))
    expect(page.locator(".groundwater-dot").first).to_be_visible()
    expect(page.locator("rect.groundwater-card")).to_have_count(0)
    expect(page.locator("polyline.groundwater-spark")).to_have_count(0)

    zoom_in(page)
    expect(page.locator("rect.groundwater-card")).to_have_count(len(STATIONS))
    expect(page.locator("polyline.groundwater-spark")).to_have_count(len(SPARKLINES))

    # ...and zooming back out puts the map into the overview again.
    page.locator(".groundwater-zoom-out").click()
    page.locator(".groundwater-zoom-out").click()
    expect(page.locator("rect.groundwater-card")).to_have_count(0)
    expect(page.locator(".groundwater-marker")).to_have_count(len(STATIONS))


def test_the_overview_dot_is_readable_and_stays_a_touch_target(
    page: Page, server_url: str
) -> None:
    """Without a card the colour of the dot is the whole message, so it has
    to be seen from two metres — while the finger target stays at 44 px."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    marker = page.locator('.groundwater-marker[data-number="16704"]')
    dot = marker.locator("circle.groundwater-dot").bounding_box()
    hit = marker.locator("rect.groundwater-hit").bounding_box()
    assert dot is not None and hit is not None
    assert dot["width"] >= 12, dot
    assert hit["width"] >= 44 and hit["height"] >= 44, hit
    # The target is centred on the dot, not floating next to it.
    assert abs((hit["x"] + hit["width"] / 2) - (dot["x"] + dot["width"] / 2)) < 1, (hit, dot)
    assert abs((hit["y"] + hit["height"] / 2) - (dot["y"] + dot["height"] / 2)) < 1, (hit, dot)


def test_tapping_a_dot_opens_the_detail_in_both_states(page: Page, server_url: str) -> None:
    """In the overview the tap is the ONLY way to a curve, so it has to work
    there just as it does once the cards are drawn."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    for state in ("overview", "zoomed"):
        page.locator('.groundwater-marker[data-number="16705"]').click()
        panel = page.locator("#groundwater-popover .groundwater-detail")
        expect(panel).to_be_visible()
        expect(panel.locator(".popover-title")).to_have_text("Freising Nord", timeout=5000)
        expect(panel.locator("path.groundwater-detail-line")).to_have_count(1)
        page.keyboard.press("Escape")
        expect(page.locator("#groundwater-popover")).to_be_hidden()
        if state == "overview":
            zoom_in(page)


def test_a_shared_well_head_stays_reachable_in_the_overview(page: Page, server_url: str) -> None:
    """Obermenzing T 3 F and T 3 T sit on one coordinate. With no cards to
    stack, their invisible tap targets are stacked instead — neither may
    swallow the other."""
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    upper = page.locator('.groundwater-marker[data-number="100"]')
    deeper = page.locator('.groundwater-marker[data-number="101"]')
    upper_hit = upper.locator("rect.groundwater-hit").bounding_box()
    deeper_hit = deeper.locator("rect.groundwater-hit").bounding_box()
    assert upper_hit is not None and deeper_hit is not None
    assert deeper_hit["y"] + deeper_hit["height"] <= upper_hit["y"] + 1, (upper_hit, deeper_hit)

    upper.click()
    expect(page.locator("#groundwater-popover .popover-title")).to_have_text("Obermenzing T 3 F")
    page.keyboard.press("Escape")
    deeper.click()
    expect(page.locator("#groundwater-popover .popover-title")).to_have_text("Obermenzing T 3 T")


def test_the_outermost_stations_sit_close_to_the_map_edge(page: Page, server_url: str) -> None:
    """Etappe 48: "zoom die karte etwas mehr rein, die äussersten messpunkte
    können nah am rand sein."

    Before this stage the default view spanned ~171 km on the kiosk while the
    stations only reach ~98 km across, so they clung to the middle of a
    mostly empty map: the rim stations covered barely half of it. The view
    now frames ~109 km, which is the 100 km circle plus a 4% margin per side.
    """
    mock_groundwater(page, stations=RIM_STATIONS)
    open_groundwater_view(page, server_url)

    map_box = page.locator(".groundwater-map").bounding_box()
    assert map_box is not None
    centres = {}
    for rim in RIM_STATIONS:
        dot = page.locator(
            f'.groundwater-marker[data-number="{rim["number"]}"] circle.groundwater-dot'
        ).bounding_box()
        assert dot is not None, rim["number"]
        # Drawn whole, not clipped by the edge — that is what the margin buys.
        assert dot["x"] >= map_box["x"], (rim["number"], dot, map_box)
        assert dot["y"] >= map_box["y"], (rim["number"], dot, map_box)
        assert dot["x"] + dot["width"] <= map_box["x"] + map_box["width"], rim["number"]
        assert dot["y"] + dot["height"] <= map_box["y"] + map_box["height"], rim["number"]
        centres[rim["number"]] = (dot["x"] + dot["width"] / 2, dot["y"] + dot["height"] / 2)

    # ...and they reach out to the border. Concrete thresholds, because
    # "looks good" is not a regression test: the same four stations spanned
    # ~53% of the width and ~55% of the height at the old default zoom.
    width_share = (centres["rim-o"][0] - centres["rim-w"][0]) / map_box["width"]
    height_share = (centres["rim-s"][1] - centres["rim-n"][1]) / map_box["height"]
    assert width_share > 0.85, width_share
    assert height_share > 0.75, height_share


def test_the_side_column_says_where_the_curves_are(page: Page, server_url: str) -> None:
    mock_groundwater(page)
    open_groundwater_view(page, server_url)

    note = page.locator(".groundwater-zoom-note")
    expect(note).to_be_visible()
    expect(note).to_have_text("Die Mini-Kurven erscheinen beim Hineinzoomen (+).")


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
    zoom_in(page)
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
    # Default is zoom 10, drawn downscaled so it frames the 50 km radius.
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/10/"))
    page.locator(".groundwater-zoom-in").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/11/"))
    # Already at the closest level.
    page.locator(".groundwater-zoom-in").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/11/"))
    page.locator(".groundwater-zoom-out").click()
    page.locator(".groundwater-zoom-out").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/9/"))
    # ...and not below the widest level either.
    page.locator(".groundwater-zoom-out").click()
    expect(first).to_have_attribute("src", re.compile(r"/tile/base/9/"))
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
