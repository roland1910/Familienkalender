"""E2E tests for the power view: mode switch, compact summary, chart.

/api/power and /api/power/history are mocked via Playwright route
interception — the E2E server has no Home Assistant behind it.
"""

import datetime as dt
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

# A "today" timestamp so the view renders just "HH:MM" (11:20 local). Built
# fresh from the local date to stay on "today" regardless of when the suite
# runs; the wall-clock time is fixed so the assertion is stable.
_TODAY_1120 = dt.datetime.now().replace(
    hour=11, minute=20, second=0, microsecond=0
).astimezone().isoformat()

POWER_PAYLOAD = {
    "production": {"value": 350.5, "available": True, "last_updated": _TODAY_1120},
    "consumption": {"value": 1487.2, "available": True, "last_updated": _TODAY_1120},
    "balance": {"value": -136.7, "available": True, "last_updated": _TODAY_1120},
    "surplus": {"value": 0.0, "available": True, "last_updated": _TODAY_1120},
    "grid_import": {"value": 136.7, "available": True, "last_updated": _TODAY_1120},
    "devices": [
        {
            "entity_id": "sensor.kuhlschrank_leistung",
            "name": "Kühlschrank",
            "value": 45.3,
            "available": True,
            "last_updated": _TODAY_1120,
            "friendly_name": "Kühlschrank Leistung",
        },
        {
            "entity_id": "sensor.schreibtisch_leistung",
            # No configured name: the HA friendly_name is shown instead.
            "name": "",
            "value": 0.0,
            "available": False,
            "last_updated": None,
            "friendly_name": "Schreibtisch Steckdose",
        },
    ],
}

SURPLUS_PAYLOAD = POWER_PAYLOAD | {
    "surplus": {"value": 213.8, "available": True},
    "grid_import": {"value": 0.0, "available": True},
}

# Two distinct history datasets so a period switch is observable: the 1-day
# window has two production points, the 1-week window has three.
_NOW_MS = int(dt.datetime.now(dt.UTC).timestamp() * 1000)


def _history(hours: int, production_points: int) -> dict:
    span = hours * 3600 * 1000
    return {
        "hours": hours,
        "production": [
            {"t": _NOW_MS - span + i * (span // production_points), "v": 100 + i * 50}
            for i in range(production_points)
        ],
        "consumption": [
            {"t": _NOW_MS - span, "v": 400},
            {"t": _NOW_MS, "v": 500},
        ],
    }


def mock_power(page: Page, payload: dict) -> None:
    page.route("**/api/power", lambda route: route.fulfill(json=payload))


def mock_history(page: Page) -> None:
    def handler(route):
        hours = 24
        match = re.search(r"hours=(\d+)", route.request.url)
        if match:
            hours = int(match.group(1))
        production_points = 3 if hours == 168 else 2
        route.fulfill(json=_history(hours, production_points))

    page.route("**/api/power/history**", handler)


def open_power_view(page: Page, server_url: str) -> None:
    goto_calendar(page, server_url)
    page.locator("#btn-mode-power").click()
    expect(page.locator(".power-view")).to_be_visible()


def test_switch_to_power_view_and_back(page: Page, server_url: str) -> None:
    mock_power(page, POWER_PAYLOAD)
    mock_history(page)
    open_power_view(page, server_url)

    expect(page.locator("#period-title")).to_have_text("Strom")
    # Calendar-only toolbar controls disappear in power mode.
    expect(page.locator("#btn-month")).to_be_hidden()
    expect(page.locator("#btn-prev")).to_be_hidden()

    # Compact summary line: production, consumption, red grid-import balance.
    summary = page.locator(".power-summary")
    expect(summary).to_be_visible()
    segments = page.locator(".power-summary-segment")
    expect(segments).to_have_count(3)
    expect(segments.nth(0)).to_contain_text("Erzeugung")
    expect(segments.nth(0)).to_contain_text("351 W")
    expect(segments.nth(1)).to_contain_text("Verbrauch")
    expect(segments.nth(1)).to_contain_text("1.487 W")
    expect(segments.nth(2)).to_have_class(re.compile("power-summary-grid"))
    expect(segments.nth(2)).to_contain_text("Netzbezug")
    expect(segments.nth(2)).to_contain_text("137 W")
    # One muted freshness stamp for the whole line (today → HH:MM).
    expect(page.locator(".power-summary-time")).to_have_text("11:20")

    # Device list with watt values; the unavailable plug is marked.
    devices = page.locator(".power-device")
    expect(devices).to_have_count(2)
    expect(devices.nth(0)).to_contain_text("Kühlschrank")
    expect(devices.nth(0)).to_contain_text("45 W")
    expect(devices.nth(1)).to_contain_text("Schreibtisch Steckdose")
    expect(devices.nth(1)).to_contain_text("nicht verfügbar")

    # Switching back restores the calendar (and its toolbar).
    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator(".power-view")).to_be_hidden()
    expect(page.locator("#btn-month")).to_be_visible()


def test_surplus_is_shown_as_green_balance_segment(page: Page, server_url: str) -> None:
    mock_power(page, SURPLUS_PAYLOAD)
    mock_history(page)
    open_power_view(page, server_url)
    balance = page.locator(".power-summary-segment").nth(2)
    expect(balance).to_have_class(re.compile("power-summary-surplus"))
    expect(balance).to_contain_text("Überschuss")
    expect(balance).to_contain_text("214 W")


def test_history_chart_renders_two_series(page: Page, server_url: str) -> None:
    mock_power(page, POWER_PAYLOAD)
    mock_history(page)
    open_power_view(page, server_url)

    chart = page.locator(".power-chart")
    expect(chart).to_be_visible()
    expect(chart.locator(".power-chart-title")).to_have_text("Erzeugung vs. Verbrauch")
    # Legend names both series.
    expect(chart).to_contain_text("Erzeugung")
    expect(chart).to_contain_text("Verbrauch")
    # Two series → two polyline paths in the SVG (default 1T = 2 prod points).
    svg = chart.locator(".power-chart-svg")
    expect(svg).to_be_visible()
    expect(svg.locator("path")).to_have_count(2)


def test_period_button_switches_the_window(page: Page, server_url: str) -> None:
    mock_power(page, POWER_PAYLOAD)
    mock_history(page)
    open_power_view(page, server_url)

    # Default is 1T (active button); switching to 1W reloads a new dataset.
    buttons = page.locator(".power-period-btn")
    expect(buttons.nth(0)).to_have_class(re.compile("power-period-active"))
    expect(buttons).to_have_count(3)

    buttons.nth(2).click()  # 1W
    expect(buttons.nth(2)).to_have_class(re.compile("power-period-active"))
    expect(buttons.nth(0)).not_to_have_class(re.compile("power-period-active"))
    # The 1W dataset still has both series (paths) drawn.
    expect(page.locator(".power-chart-svg path")).to_have_count(2)


def test_empty_history_shows_a_hint(page: Page, server_url: str) -> None:
    mock_power(page, POWER_PAYLOAD)
    page.route(
        "**/api/power/history**",
        lambda route: route.fulfill(
            json={"hours": 24, "production": [], "consumption": []}
        ),
    )
    open_power_view(page, server_url)
    expect(page.locator(".power-chart-empty")).to_have_text("Keine Verlaufsdaten")


def test_backend_error_shows_german_error_state(page: Page, server_url: str) -> None:
    page.route(
        "**/api/power",
        lambda route: route.fulfill(
            status=502, json={"detail": "Home Assistant ist nicht erreichbar."}
        ),
    )
    mock_history(page)
    goto_calendar(page, server_url)
    page.locator("#btn-mode-power").click()
    error = page.locator(".power-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("Stromdaten nicht verfügbar")
    expect(error).to_contain_text("Home Assistant ist nicht erreichbar.")
