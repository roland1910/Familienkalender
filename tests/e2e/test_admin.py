"""Browser E2E tests for the admin page core flows.

The backend is the real app with seeded demo data; only the CalDAV
connection test is intercepted in the browser (no external network).
"""

import json

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

FAKE_CALENDARS = {
    "calendars": [
        {"name": "Firma", "url": "https://cloud.example.com/dav/calendars/roland/firma/"},
        {"name": "Privat", "url": "https://cloud.example.com/dav/calendars/roland/privat/"},
    ]
}


def goto_admin(page: Page, server_url: str) -> None:
    page.goto(f"{server_url}/admin")
    expect(page.locator("#source-list .source-item").first).to_be_visible()


class TestAdminEntry:
    def test_gear_icon_leads_to_admin_and_back(self, page: Page, server_url: str) -> None:
        page.goto(server_url)
        page.locator(".admin-link").click()
        expect(page.locator("h1")).to_have_text("Verwaltung")
        expect(page.locator("#source-list .source-item")).to_have_count(3)
        page.locator(".back-link").click()
        expect(page.locator("#calendar")).to_be_visible()

    def test_source_list_shows_status_and_counts(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        names = page.locator(".source-name")
        expect(names).to_have_text(["Marina", "Kunde", "Firma"])
        marina = page.locator('.source-item[data-source-id="1"]')
        expect(marina.locator(".type-badge")).to_have_text("Google")
        expect(marina.locator(".source-status")).to_contain_text("Termine")
        expect(marina.locator(".source-status")).to_contain_text("Letzter Sync:")


class TestNextcloudWizard:
    def test_create_and_delete_a_caldav_source(self, page: Page, server_url: str) -> None:
        def fulfill_calendars(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(FAKE_CALENDARS),
            )

        page.route("**/api/admin/caldav/calendars", fulfill_calendars)
        goto_admin(page, server_url)

        page.locator("#btn-add-nextcloud").click()
        page.locator("#nc-url").fill("https://cloud.example.com")
        page.locator("#nc-username").fill("roland")
        page.locator("#nc-password").fill("app-passwort")
        page.locator("#nc-test").click()

        # Calendar list arrives, the name is prefilled from the selection.
        expect(page.locator("#nc-step-select")).to_be_visible()
        expect(page.locator("#nc-calendar option")).to_have_count(2)
        expect(page.locator("#nc-name")).to_have_value("Firma")
        page.locator("#nc-calendar").select_option(label="Privat")
        expect(page.locator("#nc-name")).to_have_value("Privat")
        page.locator("#nc-mode").select_option("filtered")
        page.locator("#nc-save").click()

        # The wizard closes and the new source appears (real backend POST).
        expect(page.locator("#nextcloud-form")).to_be_hidden()
        expect(page.locator(".source-name")).to_have_text(
            ["Marina", "Kunde", "Firma", "Privat"]
        )
        new_item = page.locator(".source-item").last
        expect(new_item.locator(".type-badge")).to_have_text("Nextcloud")

        # Delete again (two-step confirmation) to leave the shared DB clean.
        delete_button = new_item.locator(".small-button.danger")
        delete_button.click()
        expect(delete_button).to_have_text("Wirklich löschen?")
        delete_button.click()
        expect(page.locator(".source-name")).to_have_text(["Marina", "Kunde", "Firma"])

    def test_connection_error_is_shown_in_german(
        self, page: Page, server_url: str
    ) -> None:
        def fulfill_error(route: Route) -> None:
            route.fulfill(
                status=502,
                content_type="application/json",
                body=json.dumps({"detail": "Verbindung fehlgeschlagen: HTTP 401"}),
            )

        page.route("**/api/admin/caldav/calendars", fulfill_error)
        goto_admin(page, server_url)
        page.locator("#btn-add-nextcloud").click()
        page.locator("#nc-url").fill("https://cloud.example.com")
        page.locator("#nc-username").fill("roland")
        page.locator("#nc-password").fill("falsch")
        page.locator("#nc-test").click()
        expect(page.locator("#nc-error")).to_contain_text("Verbindung fehlgeschlagen")
        expect(page.locator("#nc-step-select")).to_be_hidden()


class TestSettings:
    def test_evening_boundary_roundtrip(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url)
        boundary = page.locator("#evening-boundary")
        expect(boundary).to_have_value("17:00")
        boundary.fill("18:30")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")

        page.reload()
        expect(boundary).to_have_value("18:30")

        # Reset so other tests keep seeing the default filter behaviour.
        boundary.fill("17:00")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")


class TestManualSync:
    def test_sync_button_reports_failures_of_demo_sources(
        self, page: Page, server_url: str
    ) -> None:
        # The demo sources have no real backends, so every source fails
        # quickly and locally — exactly what the result display must show.
        goto_admin(page, server_url)
        page.locator("#btn-sync").click()
        expect(page.locator("#page-message")).to_contain_text(
            "3 von 3 Quellen mit Fehlern"
        )
        expect(page.locator(".error-badge").first).to_be_visible()
        expect(page.locator(".source-error").first).to_be_visible()
