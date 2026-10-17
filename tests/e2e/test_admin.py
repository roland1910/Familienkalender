"""Browser E2E tests for the admin page core flows.

The backend is the real app with seeded demo data; only the CalDAV
connection test is intercepted in the browser (no external network).
"""

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

FAKE_CALENDARS = {
    "calendars": [
        {"name": "Firma", "url": "https://cloud.example.com/dav/calendars/roland/firma/"},
        {"name": "Privat", "url": "https://cloud.example.com/dav/calendars/roland/privat/"},
    ]
}


def goto_admin(page: Page, server_url: str, tab: str = "") -> None:
    """Open the admin page, optionally straight on one of the tabs.

    Since Etappe 51 the sections live in four tab panels, so a test that
    touches something outside "Quellen" has to say which tab it needs. The
    readiness signal is counted rather than "visible": the source list is in
    the document on every tab but only displayed on the first one.
    """
    page.goto(f"{server_url}/admin#{tab}" if tab else f"{server_url}/admin")
    expect(page.locator("#source-list .source-item").first).to_have_count(1)
    if tab:
        expect(page.locator(f"#tab-{tab}")).to_be_visible()


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


def open_source_row(page: Page, name: str):
    """Expand one source card and return its locator.

    Since Etappe 51/3 a card shows a summary and keeps its controls folded
    away; Playwright waits for visibility, so every test that touches a
    control has to open the card first.
    """
    item = page.locator(".source-item", has_text=name)
    if not item.locator(".source-controls").is_visible():
        item.locator(".source-summary").click()
    expect(item.locator(".source-controls")).to_be_visible()
    return item


class TestSourceCard:
    """Etappe 51, Schritt 3 — summary plus foldable detail.

    Roland's picture of the admin page showed the reason: seven controls
    per source in one flex row, about sixty of them on a phone screen.
    """

    def test_the_summary_is_tappable_and_opens_the_detail(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        kunde = page.locator(".source-item", has_text="Kunde")
        # Folded by default: the list stays readable at a glance.
        expect(kunde.locator(".source-controls")).to_be_hidden()
        # The summary carries name, type and the colour dot of the calendar.
        expect(kunde.locator(".source-name")).to_have_text("Kunde")
        expect(kunde.locator(".source-dot")).to_have_count(1)
        summary = kunde.locator(".source-summary")
        box = summary.bounding_box()
        assert box is not None and box["height"] >= 44, box

        summary.click()
        expect(kunde.locator(".source-controls")).to_be_visible()
        # Tapping again folds it back.
        summary.click()
        expect(kunde.locator(".source-controls")).to_be_hidden()

    def test_only_one_card_is_open_at_a_time(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url)
        open_source_row(page, "Kunde")
        open_source_row(page, "Firma")
        expect(
            page.locator(".source-item", has_text="Kunde").locator(".source-controls")
        ).to_be_hidden()

    def test_shortcode_and_priority_only_show_with_the_feed_switch(
        self, page: Page, server_url: str
    ) -> None:
        """Both only ever act inside the subscription, so they sit under its
        switch and appear with it — two fields less in the normal case."""
        goto_admin(page, server_url)
        marina = open_source_row(page, "Marina")  # full mode, feed off by default
        expect(marina.locator(".feed-toggle")).not_to_be_checked()
        expect(marina.locator(".shortcode-input")).to_be_hidden()
        expect(marina.locator(".feed-priority-input")).to_be_hidden()

        marina.locator(".feed-toggle").check()
        marina = open_source_row(page, "Marina")
        expect(marina.locator(".shortcode-input")).to_be_visible()
        expect(marina.locator(".feed-priority-input")).to_be_visible()

        # Reset so the demo data stays as the other tests expect it.
        marina.locator(".feed-toggle").uncheck()
        expect(
            open_source_row(page, "Marina").locator(".shortcode-input")
        ).to_be_hidden()

    def test_the_name_is_a_field_instead_of_a_rename_button(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        kunde = open_source_row(page, "Kunde")
        expect(kunde.get_by_role("button", name="Umbenennen")).to_have_count(0)
        field = kunde.locator(".name-input")
        expect(field).to_have_value("Kunde")
        field.fill("Kunde neu")
        field.blur()
        expect(page.locator(".source-item", has_text="Kunde neu")).to_have_count(1)

        # Reset so the demo data stays as the other tests expect it.
        back = open_source_row(page, "Kunde neu").locator(".name-input")
        back.fill("Kunde")
        back.blur()
        expect(page.locator(".source-name")).to_have_text(["Marina", "Kunde", "Firma"])

    def test_delete_stands_alone_below_a_separator(
        self, page: Page, server_url: str
    ) -> None:
        """Mis-tapping is solved structurally, not just by colour: the
        delete button shares its row with nothing else."""
        goto_admin(page, server_url)
        kunde = open_source_row(page, "Kunde")
        danger = kunde.locator(".small-button.danger")
        expect(kunde.locator(".source-danger")).to_have_count(1)
        # Nothing else in that row.
        assert kunde.locator(".source-danger button").count() == 1
        box = danger.bounding_box()
        toggle = kunde.locator(".source-toggle-button").bounding_box()
        assert box is not None and toggle is not None
        # Clearly below the enable/disable button, not beside it.
        assert box["y"] >= toggle["y"] + toggle["height"], (box, toggle)


class TestAddSourceEntry:
    """Etappe 51, Schritt 5 — one entry instead of three buttons.

    Three long buttons side by side were a wall of text for something that
    happens rarely. The entry asks the question first ("add a source?"),
    the three kinds answer it.
    """

    def test_the_three_kinds_appear_behind_one_entry(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        entry = page.locator("#btn-add-source")
        expect(entry).to_be_visible()
        box = entry.bounding_box()
        assert box is not None and box["height"] >= 44, box
        # Folded away until asked for.
        expect(page.locator("#btn-add-nextcloud")).to_be_hidden()

        entry.click()
        for selector in ["#btn-add-nextcloud", "#btn-add-google", "#btn-add-birthdays"]:
            expect(page.locator(selector)).to_be_visible()
        # The entry steps aside while the choice is up.
        expect(entry).to_be_hidden()

    def test_opening_a_wizard_from_a_tile_still_works(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        page.locator("#btn-add-source").click()
        page.locator("#btn-add-nextcloud").click()
        expect(page.locator("#nextcloud-form")).to_be_visible()


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

        # The three kinds hide behind one entry (E51/5).
        page.locator("#btn-add-source").click()
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
        # The detail folds away by default (E51/3), and opening another
        # card folds this one — so open it right before deleting.
        new_item.locator(".source-summary").click()
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
        # The three kinds hide behind one entry (E51/5).
        page.locator("#btn-add-source").click()
        page.locator("#btn-add-nextcloud").click()
        page.locator("#nc-url").fill("https://cloud.example.com")
        page.locator("#nc-username").fill("roland")
        page.locator("#nc-password").fill("falsch")
        page.locator("#nc-test").click()
        expect(page.locator("#nc-error")).to_contain_text("Verbindung fehlgeschlagen")
        expect(page.locator("#nc-step-select")).to_be_hidden()


class TestGoogleWizard:
    def test_core_flow_with_intercepted_google_endpoints(
        self, page: Page, server_url: str, server_data_dir: Path
    ) -> None:
        """Credentials → Auth-Link → Code einlösen → Kalenderwahl → Quelle anlegen.

        The Google endpoints (auth-url, connect) are intercepted in the
        browser; the parked tokens for the returned flow id are seeded
        into the server's DATA_DIR so the real POST /sources can adopt
        them. Settings PUT and source creation hit the real backend.
        """
        flow_id = "e2e-flow-123"
        pending = server_data_dir / f"google_token_pending_{flow_id}.json"
        pending.write_text(
            json.dumps({"client_id": "cid", "client_secret": "cs",
                        "refresh_token": "rt-e2e", "access_token": "at-e2e",
                        "access_token_expires_at": "2027-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )

        def fulfill_auth_url(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"}
                ),
            )

        def fulfill_connect(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "flow_id": flow_id,
                        "calendars": [
                            {"id": "m@example.com", "name": "Marina Zweitkalender"},
                            {"id": "v@example.com", "name": "Verein"},
                        ],
                    }
                ),
            )

        page.route("**/api/admin/google/auth-url", fulfill_auth_url)
        page.route("**/api/admin/google/connect", fulfill_connect)
        goto_admin(page, server_url)

        # No credentials stored yet: the wizard starts with the credentials step.
        # The three kinds hide behind one entry (E51/5).
        page.locator("#btn-add-source").click()
        page.locator("#btn-add-google").click()
        expect(page.locator("#g-step-credentials")).to_be_visible()
        page.locator("#g-client-id").fill("cid.apps.googleusercontent.com")
        page.locator("#g-client-secret").fill("cs-geheim")
        page.locator("#g-save-credentials").click()

        # Auth step: the consent link carries the (intercepted) auth URL.
        expect(page.locator("#g-step-auth")).to_be_visible()
        auth_link = page.locator("#g-auth-link")
        expect(auth_link).to_be_visible()
        expect(auth_link).to_have_attribute(
            "href", "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"
        )
        page.locator("#g-code").fill("http://localhost:1/?code=4%2F0AbCdEf&scope=x")
        page.locator("#g-connect").click()

        # Calendar list arrives, the name is prefilled from the selection.
        expect(page.locator("#g-step-select")).to_be_visible()
        expect(page.locator("#g-calendar option")).to_have_count(2)
        expect(page.locator("#g-name")).to_have_value("Marina Zweitkalender")
        page.locator("#g-calendar").select_option(label="Verein")
        expect(page.locator("#g-name")).to_have_value("Verein")
        page.locator("#g-save").click()

        # The wizard closes; the real backend created the source and
        # adopted the parked tokens.
        expect(page.locator("#google-form")).to_be_hidden()
        expect(page.locator(".source-name")).to_have_text(
            ["Marina", "Kunde", "Firma", "Verein"]
        )
        new_item = page.locator(".source-item").last
        expect(new_item.locator(".type-badge")).to_have_text("Google")
        assert not pending.exists()

        # Delete again (two-step confirmation) to leave the shared DB clean.
        # The detail folds away by default (E51/3), and opening another
        # card folds this one — so open it right before deleting.
        new_item.locator(".source-summary").click()
        delete_button = new_item.locator(".small-button.danger")
        delete_button.click()
        expect(delete_button).to_have_text("Wirklich löschen?")
        delete_button.click()
        expect(page.locator(".source-name")).to_have_text(["Marina", "Kunde", "Firma"])


class TestBirthdaysWizard:
    def test_core_flow_with_intercepted_contacts_endpoints(
        self, page: Page, server_url: str, server_data_dir: Path
    ) -> None:
        """Auth-Link (contacts) → Code einlösen → Quelle anlegen.

        The People API endpoints (contacts-auth-url, contacts-connect) are
        intercepted in the browser; the parked tokens for the returned flow
        id are seeded into the server's DATA_DIR so the real POST /sources
        can adopt them. No calendar selection — the People API has none.
        """
        flow_id = "e2e-birthdays-1"
        pending = server_data_dir / f"google_token_pending_{flow_id}.json"
        pending.write_text(
            json.dumps({"client_id": "cid", "client_secret": "cs",
                        "refresh_token": "rt-e2e", "access_token": "at-e2e",
                        "access_token_expires_at": "2027-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )

        def fulfill_auth_url(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?scope=contacts"}
                ),
            )

        def fulfill_connect(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"flow_id": flow_id}),
            )

        page.route("**/api/admin/google/contacts-auth-url", fulfill_auth_url)
        page.route("**/api/admin/google/contacts-connect", fulfill_connect)
        goto_admin(page, server_url)

        # The three kinds hide behind one entry (E51/5).
        page.locator("#btn-add-source").click()
        page.locator("#btn-add-birthdays").click()
        # Whether the wizard opens on the credentials step depends on whether
        # a prior test already stored Google credentials in the shared demo
        # DB (session-scoped server). Fill them only when that step shows.
        credentials = page.locator("#b-step-credentials")
        if credentials.is_visible():
            page.locator("#b-client-id").fill("cid.apps.googleusercontent.com")
            page.locator("#b-client-secret").fill("cs-geheim")
            page.locator("#b-save-credentials").click()

        # Auth step: the consent link carries the (intercepted) auth URL.
        expect(page.locator("#b-step-auth")).to_be_visible()
        auth_link = page.locator("#b-auth-link")
        expect(auth_link).to_have_attribute(
            "href", "https://accounts.google.com/o/oauth2/v2/auth?scope=contacts"
        )
        page.locator("#b-code").fill("http://localhost:1/?code=4%2F0AbCdEf&scope=x")
        page.locator("#b-connect").click()

        # No calendar selection and no display-mode picker (birthdays are
        # always shown "full") — straight to the name field.
        expect(page.locator("#b-step-select")).to_be_visible()
        expect(page.locator("#b-name")).to_have_value("Geburtstage")
        page.locator("#b-save").click()

        # The wizard closes; the real backend created the source and adopted
        # the parked tokens.
        expect(page.locator("#birthdays-form")).to_be_hidden()
        expect(page.locator(".source-name")).to_have_text(
            ["Marina", "Kunde", "Firma", "Geburtstage"]
        )
        new_item = page.locator(".source-item").last
        expect(new_item.locator(".type-badge")).to_have_text("Geburtstage")
        # The birthdays row shows no display-mode selector; the other
        # (google/caldav) rows still do.
        expect(new_item.locator(".mode-select")).to_have_count(0)
        kunde_item = open_source_row(page, "Kunde")
        expect(kunde_item.locator(".mode-select")).to_have_count(1)
        assert not pending.exists()

        # Delete again (two-step confirmation) to leave the shared DB clean.
        # The detail folds away by default (E51/3), and opening another
        # card folds this one — so open it right before deleting.
        new_item.locator(".source-summary").click()
        delete_button = new_item.locator(".small-button.danger")
        delete_button.click()
        expect(delete_button).to_have_text("Wirklich löschen?")
        delete_button.click()
        expect(page.locator(".source-name")).to_have_text(["Marina", "Kunde", "Firma"])


class TestSettings:
    def test_evening_boundary_roundtrip(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url, "anzeige")
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

    def test_default_view_roundtrip(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url, "anzeige")
        select = page.locator("#default-view")
        expect(select).to_have_value("month")
        select.select_option("week")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")

        page.reload()
        expect(select).to_have_value("week")

        # Reset so other tests keep starting in the month view.
        select.select_option("month")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")

    def test_screensaver_default_roundtrip(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url, "anzeige")
        select = page.locator("#screensaver-default")
        expect(select).to_have_value("off")
        select.select_option("on")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")

        page.reload()
        expect(select).to_have_value("on")

        # Reset so other tests keep the screensaver disarmed by default.
        select.select_option("off")
        page.locator("#btn-save-settings").click()
        expect(page.locator("#settings-message")).to_have_text("Gespeichert.")


class TestShortcode:
    def test_shortcode_roundtrip_in_the_source_row(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        field = open_source_row(page, "Kunde").locator(
            ".shortcode-input"
        )
        expect(field).to_have_value("")
        field.fill("rx")
        field.press("Enter")  # change event -> PATCH -> list re-render
        # The server normalizes to uppercase; the re-rendered row shows it.
        expect(
            open_source_row(page, "Kunde").locator(".shortcode-input")
        ).to_have_value("RX")

        page.reload()
        kunde_field = open_source_row(page, "Kunde").locator(
            ".shortcode-input"
        )
        expect(kunde_field).to_have_value("RX")

        # Reset so the demo data stays as other tests expect it.
        kunde_field.fill("")
        kunde_field.press("Enter")
        page.reload()
        expect(
            open_source_row(page, "Kunde").locator(".shortcode-input")
        ).to_have_value("")

    def test_invalid_shortcode_shows_german_error(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        field = open_source_row(page, "Firma").locator(
            ".shortcode-input"
        )
        field.fill("A B")
        field.press("Enter")
        expect(page.locator("#page-message")).to_contain_text("Ungültiges Kürzel")


class TestFeedPriority:
    def test_feed_priority_roundtrip_in_the_source_row(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        field = open_source_row(page, "Kunde").locator(
            ".feed-priority-input"
        )
        expect(field).to_have_value("0")
        field.fill("10")
        field.press("Enter")  # change event -> PATCH -> list re-render
        expect(
            open_source_row(page, "Kunde").locator(
                ".feed-priority-input"
            )
        ).to_have_value("10")

        page.reload()
        kunde_field = open_source_row(page, "Kunde").locator(
            ".feed-priority-input"
        )
        expect(kunde_field).to_have_value("10")

        # Reset so the demo data stays as other tests expect it.
        kunde_field.fill("0")
        kunde_field.press("Enter")
        page.reload()
        expect(
            open_source_row(page, "Kunde").locator(
                ".feed-priority-input"
            )
        ).to_have_value("0")


class TestSourceColor:
    MARINA_DEFAULT = "#d97706"  # palette color for source id 1 (colors.js)

    def _marina_color_input(self, page: Page):
        return open_source_row(page, "Marina").locator(".color-input")

    def _set_color(self, page: Page, value: str) -> None:
        # Playwright cannot type into <input type="color">; set the value
        # and fire the change event like the native picker would.
        self._marina_color_input(page).evaluate(
            "(node, value) => {"
            "  node.value = value;"
            "  node.dispatchEvent(new Event('change', { bubbles: true }));"
            "}",
            value,
        )

    def test_color_change_shows_up_in_chip_and_legend(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        expect(self._marina_color_input(page)).to_have_value(self.MARINA_DEFAULT)
        self._set_color(page, "#ff0066")
        # The PATCH re-renders the list with the stored custom color.
        expect(self._marina_color_input(page)).to_have_value("#ff0066")

        # Calendar: chip and legend dot of the source use the new color.
        page.locator(".back-link").click()
        chip = page.locator(".chip", has_text="Zahnarzt Emil")
        expect(chip).to_be_visible()
        assert (
            chip.evaluate("node => getComputedStyle(node).backgroundColor")
            == "rgb(255, 0, 102)"
        )
        legend_dot = page.locator(".legend-item", has_text="Marina").locator(".legend-dot")
        assert (
            legend_dot.evaluate("node => getComputedStyle(node).backgroundColor")
            == "rgb(255, 0, 102)"
        )

        # Reset: back to the palette default (leaves the shared DB clean).
        page.goto(f"{server_url}/admin")
        marina = open_source_row(page, "Marina")
        expect(marina.locator(".color-reset")).to_be_visible()
        marina.locator(".color-reset").click()
        expect(self._marina_color_input(page)).to_have_value(self.MARINA_DEFAULT)
        expect(
            open_source_row(page, "Marina").locator(".color-reset")
        ).to_be_hidden()


class TestFeedToggle:
    def test_feed_membership_can_be_toggled_per_source(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url)
        # Demo data: Marina is full (not in the feed), Kunde is filtered
        # (in the feed) — the migration/default behaviour.
        marina_toggle = open_source_row(page, "Marina").locator(
            ".feed-toggle"
        )
        kunde_toggle = open_source_row(page, "Kunde").locator(
            ".feed-toggle"
        )
        expect(marina_toggle).not_to_be_checked()
        expect(kunde_toggle).to_be_checked()

        kunde_toggle.click()  # opt the source out (the Valentin case)
        page.reload()
        kunde_toggle = open_source_row(page, "Kunde").locator(
            ".feed-toggle"
        )
        expect(kunde_toggle).not_to_be_checked()

        # Restore so the shared demo DB keeps its default state.
        kunde_toggle.click()
        expect(
            open_source_row(page, "Kunde").locator(".feed-toggle")
        ).to_be_checked()


class TestFeedModeWarning:
    def test_switching_to_full_mode_warns_while_feed_toggle_is_active(
        self, page: Page, server_url: str
    ) -> None:
        # Demo data: Kunde is filtered and already feeds the ICS subscription.
        # Switching it to "Alle Termine" while that toggle stays on means the
        # feed would now carry it unfiltered — the UI should warn (not block).
        goto_admin(page, server_url)
        kunde = open_source_row(page, "Kunde")
        expect(kunde.locator(".feed-toggle")).to_be_checked()
        warning = kunde.locator(".source-feed-warning")
        expect(warning).to_be_hidden()

        kunde.locator(".mode-select").select_option("full")
        expect(warning).to_be_visible()
        expect(warning).to_contain_text("vollständig")

        # Switching back to filtered clears the warning again.
        kunde.locator(".mode-select").select_option("filtered")
        expect(warning).to_be_hidden()

        # Restore so the shared demo DB keeps its default state.
        page.reload()
        kunde = open_source_row(page, "Kunde")
        expect(kunde.locator(".mode-select")).to_have_value("filtered")


class TestFeedSection:
    def test_feed_url_is_shown_and_rotates_with_confirmation(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url, "syncs")
        url_field = page.locator("#feed-url")
        expect(url_field).to_have_value(re.compile(r"/feed/[A-Za-z0-9_-]+\.ics$"))
        old_value = url_field.input_value()

        # Two-step confirmation: first click arms, second click rotates.
        rotate = page.locator("#btn-feed-rotate")
        rotate.click()
        expect(rotate).to_contain_text("Wirklich")
        rotate.click()
        expect(page.locator("#feed-message")).to_contain_text("Neuer Abo-Link")
        expect(url_field).not_to_have_value(old_value)
        expect(url_field).to_have_value(re.compile(r"/feed/[A-Za-z0-9_-]+\.ics$"))


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


class TestBusySync:
    def test_enable_and_select_source_roundtrip(
        self, page: Page, server_url: str
    ) -> None:
        """Quelle wählen + Belegt-Sync aktivieren, speichern, neu laden."""
        goto_admin(page, server_url, "syncs")
        # The section lists the demo sources as checkboxes.
        section = page.locator("section[aria-labelledby='busy-heading']")
        expect(section.locator("#busy-connected")).to_contain_text("nicht verbunden")
        toggle = section.locator("#btn-busy-toggle")
        expect(toggle).to_contain_text("Belegt-Sync ist AUS")
        expect(toggle).to_contain_text("einschalten")
        expect(toggle).to_have_attribute("aria-pressed", "false")

        first_source = section.locator(".busy-source-row").first
        first_source.locator("input[type=checkbox]").check()
        section.locator("#btn-busy-save").click()
        expect(section.locator("#busy-message")).to_contain_text("gespeichert")

        toggle.click()
        expect(section.locator("#busy-message")).to_contain_text("eingeschaltet")
        expect(toggle).to_contain_text("Belegt-Sync ist AN")
        expect(toggle).to_contain_text("ausschalten")
        expect(toggle).to_have_attribute("aria-pressed", "true")

        # Reload: the enabled toggle and the selection persist (real backend).
        page.reload()
        expect(section.locator("#btn-busy-toggle")).to_have_attribute("aria-pressed", "true")
        expect(
            section.locator(".busy-source-row input[type=checkbox]:checked")
        ).to_have_count(1)

        # Clean up so the shared DB stays neutral for other tests.
        section.locator("#btn-busy-toggle").click()
        expect(section.locator("#busy-message")).to_contain_text("ausgeschaltet")
        section.locator(".busy-source-row input:checked").first.uncheck()
        section.locator("#btn-busy-save").click()
        expect(section.locator("#busy-message")).to_contain_text("gespeichert")

    def test_write_connect_shows_auth_link(self, page: Page, server_url: str) -> None:
        """Schreib-Verbindung: Auth-Link erscheint mit der (gefakten) URL."""
        def fulfill_auth_url(route: Route) -> None:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?scope=events"}
                ),
            )

        page.route("**/api/admin/google/write-auth-url", fulfill_auth_url)
        goto_admin(page, server_url, "syncs")
        section = page.locator("section[aria-labelledby='busy-heading']")
        section.locator("#btn-busy-connect").click()
        auth_link = section.locator("#busy-auth-link")
        expect(auth_link).to_have_attribute(
            "href", "https://accounts.google.com/o/oauth2/v2/auth?scope=events"
        )
        expect(section.locator("#busy-connect-step")).to_be_visible()


class TestMirrorSync:
    def test_target_selection_and_toggle_roundtrip(
        self, page: Page, server_url: str
    ) -> None:
        """Ziel + Quelle wählen, einschalten, neu laden — alles bleibt stehen."""
        goto_admin(page, server_url, "syncs")
        section = page.locator("section[aria-labelledby='mirror-heading']")
        target = section.locator("#mirror-target")
        # Only CalDAV sources are offered as a target (demo: "Firma").
        expect(target.locator("option")).to_have_count(2)
        expect(target).to_contain_text("Firma")

        toggle = section.locator("#btn-mirror-toggle")
        expect(toggle).to_contain_text("Spiegel-Sync ist AUS")
        expect(toggle).to_have_attribute("aria-pressed", "false")

        target.select_option(label="Firma")
        # The target calendar must not offer itself as a source.
        expect(section.locator(".busy-source-row", has_text="Firma")).to_have_count(0)
        section.locator(".busy-source-row input[type=checkbox]").first.check()
        section.locator("#btn-mirror-save").click()
        expect(section.locator("#mirror-message")).to_contain_text("gespeichert")

        toggle.click()
        expect(section.locator("#mirror-message")).to_contain_text("eingeschaltet")
        expect(toggle).to_have_attribute("aria-pressed", "true")

        page.reload()
        expect(section.locator("#btn-mirror-toggle")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(section.locator("#mirror-target")).not_to_have_value("")
        expect(
            section.locator(".busy-source-row input[type=checkbox]:checked")
        ).to_have_count(1)

        # Clean up so the shared DB stays neutral for other tests.
        section.locator("#btn-mirror-toggle").click()
        expect(section.locator("#mirror-message")).to_contain_text("ausgeschaltet")
        section.locator(".busy-source-row input:checked").first.uncheck()
        section.locator("#mirror-target").select_option("")
        section.locator("#btn-mirror-save").click()
        expect(section.locator("#mirror-message")).to_contain_text("gespeichert")

    def test_deselecting_the_last_source_switches_the_mirror_off(
        self, page: Page, server_url: str
    ) -> None:
        """Ohne Quelle bleibt der Spiegel-Sync nicht heimlich an.

        Sonst wäre „keine Quelle" eine stille Anweisung, alle Kopien zu
        löschen — der Riegel im Backend verweigert das ohnehin.
        """
        goto_admin(page, server_url, "syncs")
        section = page.locator("section[aria-labelledby='mirror-heading']")
        section.locator("#mirror-target").select_option(label="Firma")
        section.locator(".busy-source-row input[type=checkbox]").first.check()
        section.locator("#btn-mirror-save").click()
        expect(section.locator("#mirror-message")).to_contain_text("gespeichert")
        section.locator("#btn-mirror-toggle").click()
        expect(section.locator("#btn-mirror-toggle")).to_have_attribute(
            "aria-pressed", "true"
        )

        section.locator(".busy-source-row input:checked").first.uncheck()
        section.locator("#btn-mirror-save").click()
        expect(section.locator("#mirror-message")).to_contain_text("ausgeschaltet")
        expect(section.locator("#btn-mirror-toggle")).to_have_attribute(
            "aria-pressed", "false"
        )

        # Clean up so the shared DB stays neutral for other tests.
        section.locator("#mirror-target").select_option("")
        section.locator("#btn-mirror-save").click()
        expect(section.locator("#mirror-message")).to_contain_text("gespeichert")

    def test_cleanup_button_needs_a_second_tap(
        self, page: Page, server_url: str
    ) -> None:
        """Der Aufräum-Knopf löscht Kopien — ein Fehltipp darf das nicht.

        Der erste Tipp schärft nur, der zweite handelt. Ohne gewähltes Ziel
        antwortet das Backend mit einer deutschen Meldung, und der Knopf
        entschärft sich wieder (die gemeinsame Demo-DB bleibt unberührt).
        """
        goto_admin(page, server_url, "syncs")
        section = page.locator("section[aria-labelledby='mirror-heading']")
        button = section.locator("#btn-mirror-cleanup")
        expect(button).to_contain_text("Kopien im Zielkalender entfernen")

        button.click()
        expect(button).to_contain_text("erneut tippen")

        button.click()
        expect(section.locator("#mirror-message")).to_contain_text(
            "kein Ziel-Kalender"
        )
        expect(button).to_contain_text("Kopien im Zielkalender entfernen")


class TestBirthdaySync:
    """Admin-Sektion des Geburtstags-Syncs.

    Die Demo-Datenbank hat bewusst keine Kontakt-Quelle (die drei Demo-Quellen
    sind zwei Google-Kalender und ein CalDAV-Kalender), deshalb prüft dieser
    Test genau den Zustand, den Roland vor dem Anlegen einer Geburtstags-Quelle
    sieht — plus den Rundlauf der beiden Ziel-Schalter.
    """

    def test_section_offers_both_targets_and_survives_a_reload(
        self, page: Page, server_url: str
    ) -> None:
        goto_admin(page, server_url, "syncs")
        section = page.locator("section[aria-labelledby='birthday-heading']")
        expect(section.locator("h2")).to_contain_text("Geburtstags-Sync")
        # Ohne Kontakt-Quelle steht dort ein deutscher Hinweis statt einer Liste.
        expect(section.locator("#birthday-source-list")).to_contain_text(
            "Noch keine Geburtstags-Quelle"
        )
        # Ziel 2: nur CalDAV-Quellen (Demo: "Firma") plus "kein Ziel".
        target = section.locator("#birthday-caldav-target")
        expect(target.locator("option")).to_have_count(2)
        expect(target).to_contain_text("Firma")

        toggle = section.locator("#btn-birthday-toggle")
        expect(toggle).to_contain_text("Geburtstags-Sync ist AUS")
        expect(toggle).to_have_attribute("aria-pressed", "false")

        # Beide Ziele sind unabhängig schaltbar.
        section.locator("#birthday-google").check()
        target.select_option(label="Firma")
        section.locator("#btn-birthday-save").click()
        expect(section.locator("#birthday-message")).to_contain_text("gespeichert")

        toggle.click()
        expect(section.locator("#birthday-message")).to_contain_text("eingeschaltet")
        expect(toggle).to_have_attribute("aria-pressed", "true")

        page.reload()
        expect(section.locator("#btn-birthday-toggle")).to_have_attribute(
            "aria-pressed", "true"
        )
        expect(section.locator("#birthday-google")).to_be_checked()
        expect(section.locator("#birthday-caldav-target")).not_to_have_value("")
        # Ohne Schreib-Verbindung sagt die Sektion, dass Xalt so nicht geht.
        expect(section.locator("#birthday-google-hint")).to_contain_text(
            "Schreib-Verbindung"
        )

        # Aufräumen, damit die gemeinsame DB für andere Tests neutral bleibt.
        section.locator("#btn-birthday-toggle").click()
        expect(section.locator("#birthday-message")).to_contain_text("ausgeschaltet")
        section.locator("#birthday-google").uncheck()
        section.locator("#birthday-caldav-target").select_option("")
        section.locator("#btn-birthday-save").click()
        expect(section.locator("#birthday-message")).to_contain_text("gespeichert")


class TestChangelog:
    def test_section_shows_seeded_entries(self, page: Page, server_url: str) -> None:
        """Das Änderungsprotokoll zeigt die Demo-Einträge beider Richtungen."""
        goto_admin(page, server_url, "durchsicht")
        section = page.locator("section[aria-labelledby='changelog-heading']")
        expect(section.locator("#changelog-list .changelog-item").first).to_be_visible()
        # Incoming and outgoing demo entries are present.
        expect(section).to_contain_text("Zahnarzt Emil")
        expect(section).to_contain_text("eingehend")
        expect(section).to_contain_text("ausgehend")
        # German action labels appear.
        expect(section).to_contain_text("hinzugefügt")

    def test_refresh_button_reloads(self, page: Page, server_url: str) -> None:
        goto_admin(page, server_url, "durchsicht")
        section = page.locator("section[aria-labelledby='changelog-heading']")
        section.locator("#btn-changelog-refresh").click()
        expect(section.locator("#changelog-message")).to_contain_text("Aktualisiert")
