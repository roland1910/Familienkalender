"""E2E tests for the weather view: mode switch, radar animation, chart.

All weather endpoints are mocked via Playwright route interception — the
E2E server has neither MET Norway nor RainViewer behind it, and the tile
proxy must never be exercised for real from a test.
"""

import base64
import datetime as dt
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

HOUR_MS = 3600000

# A 1x1 transparent PNG, served for every proxied tile.
TILE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

RADAR_FRAMES = [
    {"id": 1700000000 + index * 600, "t": (1700000000 + index * 600) * 1000} for index in range(6)
]


def _forecast(hours: int = 100) -> dict:
    """A forecast starting at the current hour, one point per hour."""
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    start = int(now.timestamp() * 1000)
    return {
        "points": [
            {
                "t": start + index * HOUR_MS,
                "temp_c": 15 + (index % 12),
                # Rain in a couple of hours so bars are actually drawn.
                "precip_mm": 1.4 if index % 7 == 3 else 0.0,
                "precip_hours": 1,
                "wind_ms": 2.0 + (index % 5),
                "wind_dir_deg": (index * 30) % 360,
            }
            for index in range(hours)
        ]
    }


def mock_weather(page: Page, *, forecast: dict | None = None, frames: list | None = None) -> None:
    page.route(
        "**/api/weather/forecast",
        lambda route: route.fulfill(json=forecast if forecast is not None else _forecast()),
    )
    page.route(
        "**/api/weather/radar/frames",
        lambda route: route.fulfill(
            json={"frames": frames if frames is not None else RADAR_FRAMES}
        ),
    )
    page.route(
        "**/api/weather/tile/**",
        lambda route: route.fulfill(body=TILE_PNG, content_type="image/png"),
    )


def open_weather_view(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-mode-weather").click()
    expect(page.locator(".weather-view")).to_be_visible()


def test_switch_to_weather_view_and_back(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    expect(page.locator("#period-title")).to_have_text("Wetter")
    # Calendar-only toolbar controls disappear, like in the power view.
    expect(page.locator("#btn-month")).to_be_hidden()
    expect(page.locator("#btn-prev")).to_be_hidden()
    expect(page.locator("#legend")).to_be_hidden()

    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator(".weather-view")).to_be_hidden()
    expect(page.locator("#btn-month")).to_be_visible()


def test_radar_shows_base_map_and_frame_layers(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    expect(page.locator(".weather-radar-title")).to_have_text("Regenradar München")
    # The base map fills the viewport with tiles (count depends on the
    # measured size, so just assert it is a plausible grid).
    base_tiles = page.locator(".weather-radar-base .weather-tile")
    expect(base_tiles.first).to_be_attached()
    assert base_tiles.count() >= 4
    # One layer per radar frame, each with its own tiles.
    expect(page.locator(".weather-radar-frame")).to_have_count(len(RADAR_FRAMES))
    assert page.locator(".weather-radar-frame").first.locator(".weather-tile").count() >= 1
    # Exactly one frame is visible at a time.
    expect(page.locator(".weather-radar-frame-active")).to_have_count(1)
    # The timestamp of the shown frame is labelled.
    expect(page.locator(".weather-radar-time")).to_contain_text("Uhr")
    # The loading hint is gone once the map is up (it would otherwise cover it).
    expect(page.locator(".weather-radar-hint")).to_be_hidden()


def test_radar_animation_advances_and_can_be_paused(page: Page, server_url: str) -> None:
    mock_weather(page)
    # Speed the animation up so the test does not wait half a second per
    # frame; the init script runs before the modules read the constant.
    page.add_init_script("window.WEATHER_FRAME_MS = 30;")
    open_weather_view(page, server_url)
    expect(page.locator(".weather-radar-frame-active")).to_have_count(1)

    def active_index() -> int:
        return page.evaluate(
            """() => {
                const layers = [...document.querySelectorAll('.weather-radar-frame')];
                return layers.findIndex((l) => l.classList.contains('weather-radar-frame-active'));
            }"""
        )

    first = active_index()
    # The animation loops, so wait for the shown frame to differ from the
    # one we saw rather than for a specific index.
    page.wait_for_function(
        """(start) => {
            const layers = [...document.querySelectorAll('.weather-radar-frame')];
            const index = layers.findIndex((l) =>
                l.classList.contains('weather-radar-frame-active'));
            return index !== start;
        }""",
        arg=first,
        timeout=10000,
    )

    # Pausing freezes the current frame.
    page.locator(".weather-radar-play").click()
    paused = active_index()
    page.wait_for_timeout(400)
    assert active_index() == paused


def test_zoom_buttons_reload_the_tile_grid(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    base_first = page.locator(".weather-radar-base .weather-tile").first
    radar_first = page.locator(".weather-radar-frame .weather-tile").first
    # Default: radar at zoom 7 (RainViewer's maximum), base one deeper.
    expect(radar_first).to_have_attribute("src", re.compile(r"/tile/radar/\d+/7/"))
    expect(base_first).to_have_attribute("src", re.compile(r"/tile/base/8/"))

    # Already at the closest level: "+" must not push past RainViewer's max.
    page.locator(".weather-zoom-in").click()
    expect(radar_first).to_have_attribute("src", re.compile(r"/tile/radar/\d+/7/"))

    page.locator(".weather-zoom-out").click()
    expect(radar_first).to_have_attribute("src", re.compile(r"/tile/radar/\d+/6/"))
    expect(base_first).to_have_attribute("src", re.compile(r"/tile/base/7/"))
    page.locator(".weather-zoom-out").click()
    expect(radar_first).to_have_attribute("src", re.compile(r"/tile/radar/\d+/5/"))
    # ... and not below the widest level either.
    page.locator(".weather-zoom-out").click()
    expect(radar_first).to_have_attribute("src", re.compile(r"/tile/radar/\d+/5/"))
    # The map is still fully tiled after zooming.
    assert page.locator(".weather-radar-base .weather-tile").count() >= 4


def test_forecast_chart_renders_temperature_precipitation_and_wind(
    page: Page, server_url: str
) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    chart = page.locator(".weather-chart")
    expect(chart.locator(".weather-chart-title")).to_have_text("Vorhersage München")
    expect(chart).to_contain_text("Temperatur")
    expect(chart).to_contain_text("Niederschlag")
    expect(chart).to_contain_text("Wind")

    svg = chart.locator(".weather-chart-svg")
    expect(svg).to_be_visible()
    # One temperature polyline, precipitation bars and wind arrow groups.
    expect(svg.locator("path.weather-temp-line")).to_have_count(1)
    assert svg.locator("rect").count() > 0
    assert svg.locator("g").count() > 0


def test_period_button_switches_between_24h_48h_and_96h(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    buttons = page.locator(".weather-period-btn")
    expect(buttons).to_have_count(3)
    expect(buttons.nth(0)).to_have_text("24 h")
    expect(buttons.nth(1)).to_have_text("48 h")
    expect(buttons.nth(2)).to_have_text("96 h")
    # 96 h is the default (Etappe 37): active without any tap.
    expect(buttons.nth(2)).to_have_class(re.compile("weather-period-active"))
    expect(buttons.nth(0)).not_to_have_class(re.compile("weather-period-active"))

    def day_labels() -> list[str]:
        return page.locator(".weather-chart-svg .weather-day-label").all_text_contents()

    # The wide default window is split into named days.
    assert len(day_labels()) >= 4, day_labels()

    buttons.nth(0).click()
    expect(buttons.nth(0)).to_have_class(re.compile("weather-period-active"))
    expect(buttons.nth(2)).not_to_have_class(re.compile("weather-period-active"))
    # A single day window shows at most today and tomorrow.
    assert len(day_labels()) <= 2, day_labels()


def test_chart_separates_the_days_and_labels_them(page: Page, server_url: str) -> None:
    """Roland's Etappe-38 feedback: "in der grafik des wetterforcasts fehlen
    mir vertikale linien die die tage abgrenzen"."""
    mock_weather(page)
    open_weather_view(page, server_url)
    svg = page.locator(".weather-chart-svg")

    # One separator per local midnight in the 96h window (3 or 4, depending
    # on the time of day the test runs), each a full-height vertical line.
    lines = svg.locator("line.weather-day-line")
    assert lines.count() >= 3, lines.count()
    for index in range(lines.count()):
        line = lines.nth(index)
        assert line.get_attribute("x1") == line.get_attribute("x2")

    # Each day carries a German weekday + date label above the chart.
    labels = svg.locator(".weather-day-label").all_text_contents()
    assert len(labels) >= 4, labels
    weekday_date = re.compile(r"^(Mo|Di|Mi|Do|Fr|Sa|So) \d\d\.\d\d\.$")
    assert all(weekday_date.match(label) for label in labels), labels

    # The x axis ticks are round clock times, not the crooked raw point
    # times ("Do 03:12") the chart used to print.
    hours = svg.locator(".weather-hour-label").all_text_contents()
    assert hours, "no hour ticks"
    assert all(re.fullmatch(r"(00|12):00", label) for label in hours), hours

    # Nights are shaded, which is what makes the day rhythm readable.
    assert svg.locator("rect.weather-night").count() >= 3


def test_precipitation_bars_are_clearly_visible_when_it_rains(
    page: Page, server_url: str
) -> None:
    """A wet forecast must actually show bars — including a small amount,
    which used to disappear into a hairline."""
    now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    start = int(now.timestamp() * 1000)
    points = []
    for index in range(100):
        # Steady drizzle, a downpour, and one very small amount.
        if index % 6 == 0:
            precip = 4.0
        elif index % 6 == 3:
            precip = 0.1
        else:
            precip = 0.6
        points.append(
            {
                "t": start + index * HOUR_MS,
                "temp_c": 14 + (index % 8),
                "precip_mm": precip,
                "precip_hours": 1,
                "wind_ms": 3.0,
                "wind_dir_deg": 200,
            }
        )
    mock_weather(page, forecast={"points": points})
    open_weather_view(page, server_url)

    bars = page.locator(".weather-chart-svg rect.weather-precip-bar")
    assert bars.count() > 50, bars.count()
    heights = [
        float(bars.nth(index).get_attribute("height")) for index in range(min(bars.count(), 20))
    ]
    # Every bar is drawn at least a few user units high, and the downpour is
    # clearly taller than the drizzle.
    assert min(heights) >= 3, heights
    assert max(heights) > min(heights) * 2, heights
    # The bars stay inside the lower part of the plot: the temperature line
    # must remain readable above even the wettest hour.
    tops = [float(bars.nth(index).get_attribute("y")) for index in range(min(bars.count(), 20))]
    assert min(tops) > 100, tops


def test_weather_view_fits_the_kiosk_without_scrolling(page: Page, server_url: str) -> None:
    """On the 1920x1080 kiosk the whole weather view (radar + chart) fits the
    screen; Roland must not have to scroll to see the forecast graph."""
    mock_weather(page)
    page.set_viewport_size({"width": 1920, "height": 1080})
    open_weather_view(page, server_url)

    # Both halves are present and the chart SVG is on screen.
    expect(page.locator(".weather-radar-map")).to_be_visible()
    expect(page.locator(".weather-chart-svg")).to_be_visible()

    # The weather section does not overflow its own box (no scrollbar).
    overflow = page.eval_on_selector(
        "#weather",
        "el => el.scrollHeight - el.clientHeight",
    )
    assert overflow <= 1, overflow

    # The chart SVG's bottom edge sits inside the viewport height.
    svg_bottom = page.eval_on_selector(
        ".weather-chart-svg", "el => el.getBoundingClientRect().bottom"
    )
    assert svg_bottom <= 1080 + 1, svg_bottom


def test_attribution_names_all_three_data_sources(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)
    attribution = page.locator(".weather-attribution")
    expect(attribution).to_contain_text("MET Norway")
    expect(attribution).to_contain_text("RainViewer")
    expect(attribution).to_contain_text("OpenStreetMap")


def test_forecast_error_shows_a_german_message(page: Page, server_url: str) -> None:
    page.route(
        "**/api/weather/forecast",
        lambda route: route.fulfill(
            status=502, json={"detail": "Der Wetterdienst ist nicht erreichbar."}
        ),
    )
    page.route(
        "**/api/weather/radar/frames",
        lambda route: route.fulfill(json={"frames": RADAR_FRAMES}),
    )
    page.route(
        "**/api/weather/tile/**",
        lambda route: route.fulfill(body=TILE_PNG, content_type="image/png"),
    )
    open_weather_view(page, server_url)
    expect(page.locator(".weather-chart-empty")).to_have_text(
        "Der Wetterdienst ist nicht erreichbar."
    )
    # The radar half still works — one failure must not take the view down.
    expect(page.locator(".weather-radar-frame")).to_have_count(len(RADAR_FRAMES))


def test_radar_error_shows_a_german_hint(page: Page, server_url: str) -> None:
    page.route("**/api/weather/forecast", lambda route: route.fulfill(json=_forecast()))
    page.route(
        "**/api/weather/radar/frames",
        lambda route: route.fulfill(
            status=502, json={"detail": "Der Regenradar-Dienst ist nicht erreichbar."}
        ),
    )
    open_weather_view(page, server_url)
    hint = page.locator(".weather-radar-hint")
    expect(hint).to_be_visible()
    expect(hint).to_have_text("Der Regenradar-Dienst ist nicht erreichbar.")
    # The chart still renders.
    expect(page.locator(".weather-chart-svg")).to_be_visible()


def test_empty_forecast_shows_a_hint_instead_of_crashing(page: Page, server_url: str) -> None:
    mock_weather(page, forecast={"points": []})
    open_weather_view(page, server_url)
    expect(page.locator(".weather-chart-empty")).to_have_text("Keine Vorhersagedaten")


def test_weather_mode_survives_a_reload(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)
    page.reload()
    # view-memory.js restores the mode, so the weather view comes straight up.
    expect(page.locator(".weather-view")).to_be_visible()
    expect(page.locator("#period-title")).to_have_text("Wetter")
