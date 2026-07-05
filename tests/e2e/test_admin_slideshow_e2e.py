"""Browser E2E test for the admin "Diashow (Kiosk)" section.

The slideshow admin endpoints are intercepted in the browser so the test
does not depend on a real /media share behind the E2E server.
"""

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e


def _route_slideshow(page: Page) -> None:
    state = {"dirs": [], "photo_count": 0}

    def handle_get(route: Route) -> None:
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_put(route: Route) -> None:
        import json as _json

        body = _json.loads(route.request.post_data or "{}")
        state["dirs"] = body.get("dirs", [])
        state["photo_count"] = 7 if state["dirs"] else 0
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_rescan(route: Route) -> None:
        state["photo_count"] = 12
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_dirs(route: Route) -> None:
        route.fulfill(
            json={
                "base": "/media",
                "dirs": [
                    {"name": "Familie", "path": "/media/Familie"},
                    {"name": "Urlaube", "path": "/media/Urlaube"},
                ],
            }
        )

    # More specific routes first (Playwright matches most-recently-added).
    page.route("**/api/admin/slideshow/rescan", handle_rescan)
    page.route("**/api/admin/slideshow/dirs**", handle_dirs)
    page.route(
        "**/api/admin/slideshow",
        lambda route: handle_put(route)
        if route.request.method == "PUT"
        else handle_get(route),
    )


def _goto_admin(page: Page, server_url: str) -> None:
    page.goto(f"{server_url}/admin")
    expect(page.locator("#slideshow-heading")).to_be_visible()


def test_add_directory_and_rescan(page: Page, server_url: str) -> None:
    _route_slideshow(page)
    _goto_admin(page, server_url)

    # Initially empty index and no configured directories.
    expect(page.locator("#slideshow-count")).to_have_text("0")
    expect(page.locator("#slideshow-dir-list")).to_contain_text("Noch keine Ordner")

    # The browse dropdown is populated from the (mocked) /media listing.
    expect(page.locator("#slideshow-browse option")).to_have_count(2)

    # Add the first directory; the count updates from the PUT response.
    page.locator("#slideshow-browse").select_option("/media/Familie")
    page.locator("#btn-slideshow-add").click()
    expect(page.locator("#slideshow-count")).to_have_text("7")
    expect(page.locator("#slideshow-dir-list .slideshow-dir")).to_have_count(1)
    expect(page.locator("#slideshow-dir-list .slideshow-dir-name")).to_have_text("Familie")

    # Rescan reports a fresh count.
    page.locator("#btn-slideshow-rescan").click()
    expect(page.locator("#slideshow-count")).to_have_text("12")

    # Remove the directory again.
    page.locator("#slideshow-dir-list .action-button.subtle").click()
    expect(page.locator("#slideshow-count")).to_have_text("0")
    expect(page.locator("#slideshow-dir-list")).to_contain_text("Noch keine Ordner")
