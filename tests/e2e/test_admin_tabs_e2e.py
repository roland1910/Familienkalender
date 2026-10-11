"""Browser E2E tests for the admin tab shell (Etappe 51, step 1).

The sections of the admin page are grouped into four tab panels. Switching
runs over `location.hash` and real links, so the back button, deep links and
a reload have to keep working — and, most importantly, the whole thing is
progressive enhancement: without the module every section stays visible.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

pytestmark = pytest.mark.e2e

TAB_LABELS = ["Quellen", "Syncs & Abo", "Anzeige", "Durchsicht"]


def _goto(page: Page, server_url: str, hash_: str = "") -> None:
    page.goto(f"{server_url}/admin{hash_}")
    # Set by admin-nav.js; until then nothing on the page is hidden.
    expect(page.locator("body")).to_have_attribute("data-tabs", "on")


def _tab(page: Page, hash_: str):
    return page.locator(f"#admin-tabs a[data-tab='{hash_}']")


class TestTabShell:
    def test_the_four_tabs_are_there_and_are_touch_sized(
        self, page: Page, server_url: str
    ) -> None:
        _goto(page, server_url)
        tabs = page.locator("#admin-tabs .admin-tab")
        expect(tabs).to_have_count(4)
        expect(tabs).to_have_text(TAB_LABELS)
        for index in range(4):
            box = tabs.nth(index).bounding_box()
            assert box is not None and box["height"] >= 44, (index, box)

    def test_sources_is_the_default_and_the_other_panels_are_hidden(
        self, page: Page, server_url: str
    ) -> None:
        _goto(page, server_url)
        expect(page.locator("#tab-quellen")).to_be_visible()
        for other in ("syncs", "anzeige", "durchsicht"):
            expect(page.locator(f"#tab-{other}")).to_be_hidden()
        expect(_tab(page, "quellen")).to_have_attribute("aria-current", "page")

    def test_clicking_a_tab_switches_the_panel_and_sets_the_hash(
        self, page: Page, server_url: str
    ) -> None:
        _goto(page, server_url)
        _tab(page, "syncs").click()
        expect(page).to_have_url(re.compile(r"#syncs$"))
        expect(page.locator("#tab-syncs")).to_be_visible()
        expect(page.locator("#busy-heading")).to_be_visible()
        expect(page.locator("#tab-quellen")).to_be_hidden()
        expect(_tab(page, "syncs")).to_have_attribute("aria-current", "page")
        expect(_tab(page, "quellen")).not_to_have_attribute("aria-current", "page")

    def test_a_reload_lands_on_the_same_tab(self, page: Page, server_url: str) -> None:
        _goto(page, server_url, "#syncs")
        expect(page.locator("#tab-syncs")).to_be_visible()
        page.reload()
        expect(page.locator("body")).to_have_attribute("data-tabs", "on")
        expect(page.locator("#tab-syncs")).to_be_visible()
        expect(page.locator("#tab-quellen")).to_be_hidden()

    def test_the_back_button_returns_to_the_previous_tab(
        self, page: Page, server_url: str
    ) -> None:
        _goto(page, server_url)
        _tab(page, "syncs").click()
        expect(page.locator("#tab-syncs")).to_be_visible()
        _tab(page, "anzeige").click()
        expect(page.locator("#tab-anzeige")).to_be_visible()

        page.go_back()
        expect(page.locator("#tab-syncs")).to_be_visible()
        expect(page.locator("#tab-anzeige")).to_be_hidden()
        page.go_back()
        expect(page.locator("#tab-quellen")).to_be_visible()

    def test_a_deep_link_to_a_section_opens_its_tab_and_scrolls_there(
        self, page: Page, server_url: str
    ) -> None:
        # Deliberately short, so the Anzeige panel is taller than the window
        # and the jump has to scroll for real.
        page.set_viewport_size({"width": 900, "height": 600})
        _goto(page, server_url, "#slideshow-heading")

        expect(page.locator("#tab-anzeige")).to_be_visible()
        expect(page.locator("#tab-quellen")).to_be_hidden()
        heading = page.locator("#slideshow-heading")
        expect(heading).to_be_in_viewport()
        assert page.evaluate("window.scrollY") > 0

        # scroll-margin-top keeps it clear of the sticky tab bar.
        bar = page.locator("#admin-tabs").bounding_box()
        box = heading.bounding_box()
        assert bar is not None and box is not None
        assert box["y"] >= bar["y"] + bar["height"] - 1, (box, bar)

    def test_an_unknown_hash_falls_back_to_the_sources_tab(
        self, page: Page, server_url: str
    ) -> None:
        _goto(page, server_url, "#gibt-es-nicht")
        expect(page.locator("#tab-quellen")).to_be_visible()
        expect(page.locator("#tab-durchsicht")).to_be_hidden()
        expect(_tab(page, "quellen")).to_have_attribute("aria-current", "page")

    def test_without_javascript_every_section_stays_visible(
        self, browser: Browser, server_url: str
    ) -> None:
        """The property that matters most: a broken or missing admin-nav.js
        leaves the long page of today standing — never an empty one."""
        context = browser.new_context(java_script_enabled=False)
        try:
            page = context.new_page()
            page.goto(f"{server_url}/admin")
            for heading in (
                "#sources-heading",
                "#settings-heading",
                "#slideshow-heading",
                "#pending-deletes-heading",
                "#feed-heading",
                "#busy-heading",
                "#mirror-heading",
                "#birthday-heading",
                "#changelog-heading",
            ):
                expect(page.locator(heading)).to_be_visible()
            # The bar itself stays out of the way: links that cannot switch
            # anything would only mislead.
            expect(page.locator("#admin-tabs")).to_be_hidden()
        finally:
            context.close()

    def test_the_tab_bar_fits_a_phone_in_two_rows(
        self, page: Page, server_url: str
    ) -> None:
        page.set_viewport_size({"width": 360, "height": 780})
        _goto(page, server_url)
        tabs = page.locator("#admin-tabs .admin-tab")
        rows = sorted({round(tabs.nth(i).bounding_box()["y"]) for i in range(4)})
        assert len(rows) == 2, rows
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, overflow


class TestGlobalMessage:
    def test_an_error_from_a_hidden_section_is_still_shown(
        self, page: Page, server_url: str
    ) -> None:
        """#page-message sits ABOVE the tab bar on purpose: the change log
        lives in "Durchsicht", but its failure has to be visible from the
        tab that is open."""
        page.route(
            "**/api/admin/changelog",
            lambda route: route.fulfill(
                status=500, json={"detail": "Protokoll nicht lesbar."}
            ),
        )
        _goto(page, server_url)
        expect(page.locator("#tab-durchsicht")).to_be_hidden()
        message = page.locator("#page-message")
        expect(message).to_be_visible()
        expect(message).to_have_text("Protokoll nicht lesbar.")
        expect(message).to_be_in_viewport()
