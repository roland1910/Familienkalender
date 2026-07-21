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


def _forecast(hours: int = 49) -> dict:
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


def test_period_button_switches_between_24h_and_48h(page: Page, server_url: str) -> None:
    mock_weather(page)
    open_weather_view(page, server_url)

    buttons = page.locator(".weather-period-btn")
    expect(buttons).to_have_count(2)
    expect(buttons.nth(0)).to_have_text("24 h")
    expect(buttons.nth(0)).to_have_class(re.compile("weather-period-active"))

    weekday = re.compile(r"^(Mo|Di|Mi|Do|Fr|Sa|So) \d\d:\d\d$")

    def axis_labels() -> list[str]:
        return page.locator(".weather-chart-svg text").all_text_contents()

    # The 24h window labels its time axis with a plain clock ...
    assert not any(weekday.match(label) for label in axis_labels())

    buttons.nth(1).click()
    expect(buttons.nth(1)).to_have_class(re.compile("weather-period-active"))
    expect(buttons.nth(0)).not_to_have_class(re.compile("weather-period-active"))
    # ... the 48h window prefixes the weekday so the second day is clear.
    assert any(weekday.match(label) for label in axis_labels())


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
