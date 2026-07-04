"""Browser E2E tests for the admin gate (Etappe 8).

Non-admin requests are simulated by sending an X-Remote-User-Id header
with every browser request — exactly what the Supervisor ingress proxy
does for a logged-in HA user. The test server cannot resolve the admin
group for that user (HA_WS_URL points nowhere, see conftest), so the
backend treats the user as non-admin (fail closed). Header-less requests
from 127.0.0.1 count as admin, which keeps the rest of the suite green.
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = pytest.mark.e2e


@pytest.fixture
def non_admin_page(
    browser: Browser, browser_context_args: dict, server_url: str
) -> Iterator[Page]:
    """A page whose every request carries a (non-admin) HA user header."""
    context = browser.new_context(
        **browser_context_args,
        extra_http_headers={"X-Remote-User-Id": "e2e-nicht-admin"},
    )
    yield context.new_page()
    context.close()


class TestNonAdminUser:
    def test_calendar_works_but_gear_is_hidden(
        self, non_admin_page: Page, server_url: str
    ) -> None:
        non_admin_page.goto(server_url)
        # The calendar itself stays fully usable for normal HA users.
        expect(non_admin_page.locator(".month-grid")).to_be_visible()
        expect(non_admin_page.locator(".day-cell")).to_have_count(42)
        # The gear never appears: hidden by default, only revealed after
        # GET api/me confirms admin — which it does not here.
        expect(non_admin_page.locator(".admin-link")).to_be_hidden()

    def test_direct_admin_url_shows_german_403_page(
        self, non_admin_page: Page, server_url: str
    ) -> None:
        response = non_admin_page.goto(f"{server_url}/admin")
        assert response is not None and response.status == 403
        expect(non_admin_page.locator("h1")).to_have_text("Nur für Administratoren.")
        expect(non_admin_page.locator("a")).to_contain_text("Zurück zum Kalender")

    def test_admin_api_is_blocked(self, non_admin_page: Page, server_url: str) -> None:
        response = non_admin_page.request.get(f"{server_url}/api/admin/sources")
        assert response.status == 403


class TestAdminUser:
    def test_gear_becomes_visible_for_admin(self, page: Page, server_url: str) -> None:
        # The default context sends no user headers; from 127.0.0.1 the
        # backend treats that as admin (local dev fallback).
        page.goto(server_url)
        expect(page.locator(".admin-link")).to_be_visible()
