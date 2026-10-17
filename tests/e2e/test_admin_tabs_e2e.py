"""Browser E2E tests for the admin tab shell (Etappe 51, steps 1 and 2).

The sections of the admin page are grouped into four tab panels. Switching
runs over `location.hash` and real links, so the back button, deep links and
a reload have to keep working — and, most importantly, the whole thing is
progressive enhancement: without the module every section stays visible.
"""

import re

import pytest
from playwright.sync_api import Browser, Page, Route, expect

pytestmark = pytest.mark.e2e

TAB_LABELS = ["Quellen", "Syncs & Abo", "Anzeige", "Durchsicht"]


def _goto(page: Page, server_url: str, hash_: str = "") -> None:
    page.goto(f"{server_url}/admin{hash_}")
    # Set by admin-nav.js; until then nothing on the page is hidden.
    expect(page.locator("body")).to_have_attribute("data-tabs", "on")


def _tab(page: Page, hash_: str):
    return page.locator(f"#admin-tabs a[data-tab='{hash_}']")


def _badge(page: Page, hash_: str):
    return _tab(page, hash_).locator(".admin-tab-badge")


def _source(**overrides) -> dict:
    source = {
        "id": 1,
        "type": "google",
        "name": "Marina",
        "enabled": True,
        "display_mode": "full",
        "shortcode": "",
        "color": "",
        "include_in_feed": False,
        "feed_priority": 0,
        "config": {},
        "last_sync_at": "2026-10-11T08:00:00+00:00",
        "last_sync_error": None,
        "event_count": 5,
    }
    return source | overrides


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
        # and the jump has to scroll for real. Narrow on purpose too: from
        # 900px up the bar becomes a left sidebar (Schritt 4) and cannot
        # cover anything, so the scroll-margin check below only has
        # something to prove in the stacked layout.
        page.set_viewport_size({"width": 700, "height": 600})
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


class TestStatusBadges:
    def test_the_source_error_dot_appears_and_disappears(
        self, page: Page, server_url: str
    ) -> None:
        state = {"failing": True}

        def handle_sources(route: Route) -> None:
            error = "Verbindung fehlgeschlagen." if state["failing"] else None
            route.fulfill(json={"sources": [_source(last_sync_error=error)]})

        page.route("**/api/admin/sources", handle_sources)
        page.route("**/api/sync", lambda route: route.fulfill(json={"results": {"1": None}}))

        _goto(page, server_url)
        badge = _badge(page, "quellen")
        expect(badge).to_be_visible()
        expect(badge).to_have_class(re.compile(r"admin-tab-badge-error"))
        # Not colour alone: the screen reader hears the count.
        expect(_tab(page, "quellen")).to_have_attribute("aria-label", "Quellen, 1 Fehler")

        # The manual sync re-renders the list — now without an error.
        state["failing"] = False
        page.locator("#btn-sync").click()
        expect(badge).to_be_hidden()
        expect(_tab(page, "quellen")).not_to_have_attribute("aria-label", re.compile(r".*"))

    def test_the_review_counter_appears_and_disappears(
        self, page: Page, server_url: str
    ) -> None:
        state = {
            "items": [
                {"id": 1, "name": "a.jpg", "path": "Photos/a.jpg", "marked_at": None},
                {"id": 2, "name": "b.jpg", "path": "Photos/b.jpg", "marked_at": None},
            ]
        }
        page.route(
            "**/api/admin/slideshow/pending-deletes",
            lambda route: route.fulfill(json={"items": state["items"]}),
        )
        page.route("**/api/slideshow/image/**", lambda route: route.fulfill(status=404))

        _goto(page, server_url, "#durchsicht")
        badge = _badge(page, "durchsicht")
        expect(badge).to_be_visible()
        # The number itself, not just a dot — and spelled out for the reader.
        expect(badge).to_have_text("2")
        expect(_tab(page, "durchsicht")).to_have_attribute(
            "aria-label", "Durchsicht, 2 Fotos vorgemerkt"
        )

        state["items"] = []
        page.locator("#btn-pending-deletes-reload").click()
        expect(page.locator("#pending-delete-list")).to_contain_text("Keine Fotos")
        expect(badge).to_be_hidden()


def test_on_a_wide_screen_the_tabs_become_a_left_sidebar(
    page: Page, server_url: str
) -> None:
    """Etappe 51, Schritt 4 — reines CSS, kein Javascript.

    Auf dem Desktop ist eine waagerechte Leiste über dem Inhalt
    verschenkter Platz: daneben liegt eine ganze Bildschirmbreite brach,
    und der Inhalt beginnt weiter unten. Dieselbe `<nav>` wird deshalb ab
    900 px zur Spalte links; am Handy bleibt alles, wie es war.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    _goto(page, server_url)

    nav = page.locator("#admin-tabs").bounding_box()
    panel = page.locator("#tab-quellen").bounding_box()
    assert nav is not None and panel is not None
    # Side by side: the bar ends before the panel begins.
    assert nav["x"] + nav["width"] <= panel["x"] + 1, (nav, panel)
    # And it really is a column, not a row.
    first = page.locator("#admin-tabs a").first.bounding_box()
    second = page.locator("#admin-tabs a").nth(1).bounding_box()
    assert first is not None and second is not None
    assert second["y"] >= first["y"] + first["height"] - 1, (first, second)
    # Every tab stays a comfortable target.
    assert first["height"] >= 44, first


def test_on_a_narrow_screen_the_tabs_stay_a_bar_on_top(
    page: Page, server_url: str
) -> None:
    page.set_viewport_size({"width": 390, "height": 800})
    _goto(page, server_url)

    nav = page.locator("#admin-tabs").bounding_box()
    panel = page.locator("#tab-quellen").bounding_box()
    assert nav is not None and panel is not None
    assert nav["y"] + nav["height"] <= panel["y"] + 1, (nav, panel)
    # Two rows of two, never a sideways scroll.
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth + 1"
    )
