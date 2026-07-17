"""E2E tests for the server-side default view (admin setting default_view).

A device without a stored per-device view (fresh browser context = empty
localStorage - exactly the kiosk after a restart) starts in the configured
default view; a deliberate per-device choice always wins over the server
default. The setting is flipped via the admin API (localhost = admin in
this suite) and reset afterwards so the other E2E files keep starting in
the month view.
"""

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e.helpers import goto_calendar

pytestmark = pytest.mark.e2e

ACTIVE = re.compile(r"\bactive\b")


def _put_default_view(server_url: str, view: str) -> None:
    response = httpx.put(
        f"{server_url}/api/admin/settings",
        json={"evening_boundary": "17:00", "default_view": view},
        timeout=10.0,
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def week_as_server_default(server_url: str) -> Iterator[None]:
    _put_default_view(server_url, "week")
    yield
    _put_default_view(server_url, "month")


def test_server_default_week_starts_in_week_view(
    week_as_server_default: None, page: Page, server_url: str
) -> None:
    # Fresh context, no interaction: the very first render is the week view.
    goto_calendar(page, server_url)
    expect(page.locator("#calendar .week-view")).to_be_visible()
    expect(page.locator("#btn-week")).to_have_class(ACTIVE)


def test_saved_device_choice_wins_over_server_default(
    week_as_server_default: None, page: Page, server_url: str
) -> None:
    goto_calendar(page, server_url)
    # Deliberate user choice: month view (persisted per device).
    page.locator("#btn-month").click()
    expect(page.locator("#calendar .month-view")).to_be_visible()

    # The server default (week) must NOT override it on reload.
    page.reload()
    expect(page.locator("#calendar .month-view")).to_be_visible()
    expect(page.locator("#btn-month")).to_have_class(ACTIVE)
