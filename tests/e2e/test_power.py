"""E2E tests for the power view: mode switch, values, error state.

/api/power is mocked via Playwright route interception — the E2E server
has no Home Assistant behind it.
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


def mock_power(page: Page, payload: dict) -> None:
    page.route("**/api/power", lambda route: route.fulfill(json=payload))


def test_switch_to_power_view_and_back(page: Page, server_url: str) -> None:
    mock_power(page, POWER_PAYLOAD)
    goto_calendar(page, server_url)

    page.locator("#btn-mode-power").click()
    expect(page.locator(".power-view")).to_be_visible()
    expect(page.locator("#period-title")).to_have_text("Strom")
    # Calendar-only toolbar controls disappear in power mode.
    expect(page.locator("#btn-month")).to_be_hidden()
    expect(page.locator("#btn-prev")).to_be_hidden()

    # Tiles: production, consumption, red grid-import balance (German format).
    tiles = page.locator(".power-tile")
    expect(tiles).to_have_count(3)
    expect(tiles.nth(0)).to_contain_text("Erzeugung")
    expect(tiles.nth(0)).to_contain_text("351 W")
    expect(tiles.nth(1)).to_contain_text("Verbrauch")
    expect(tiles.nth(1)).to_contain_text("1.487 W")
    expect(tiles.nth(2)).to_have_class(re.compile("power-tile-grid"))
    expect(tiles.nth(2)).to_contain_text("Netzbezug")
    expect(tiles.nth(2)).to_contain_text("137 W")

    # Device list with watt values; the unavailable plug is marked.
    devices = page.locator(".power-device")
    expect(devices).to_have_count(2)
    expect(devices.nth(0)).to_contain_text("Kühlschrank")
    expect(devices.nth(0)).to_contain_text("45 W")
    # No configured name → the HA friendly_name is shown for the second row.
    expect(devices.nth(1)).to_contain_text("Schreibtisch Steckdose")
    expect(devices.nth(1)).to_contain_text("nicht verfügbar")

    # The "as of" time appears small under the first device (today → HH:MM).
    expect(devices.nth(0).locator(".power-device-time")).to_have_text("11:20")
    # The unavailable device has no last_updated → no timestamp rendered.
    expect(devices.nth(1).locator(".power-device-time")).to_have_count(0)
    # The production tile also shows its freshness time.
    expect(tiles.nth(0).locator(".power-tile-time")).to_have_text("11:20")

    # Switching back restores the calendar (and its toolbar).
    page.locator("#btn-mode-calendar").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator(".power-view")).to_be_hidden()
    expect(page.locator("#btn-month")).to_be_visible()


def test_surplus_is_shown_as_green_balance_tile(page: Page, server_url: str) -> None:
    mock_power(page, SURPLUS_PAYLOAD)
    goto_calendar(page, server_url)
    page.locator("#btn-mode-power").click()
    balance = page.locator(".power-tile").nth(2)
    expect(balance).to_have_class(re.compile("power-tile-surplus"))
    expect(balance).to_contain_text("Überschuss")
    expect(balance).to_contain_text("214 W")


def test_backend_error_shows_german_error_state(page: Page, server_url: str) -> None:
    page.route(
        "**/api/power",
        lambda route: route.fulfill(
            status=502, json={"detail": "Home Assistant ist nicht erreichbar."}
        ),
    )
    goto_calendar(page, server_url)
    page.locator("#btn-mode-power").click()
    error = page.locator(".power-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("Stromdaten nicht verfügbar")
    expect(error).to_contain_text("Home Assistant ist nicht erreichbar.")
