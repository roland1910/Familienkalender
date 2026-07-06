"""Browser E2E test for the admin "Diashow (Kiosk)" section.

The slideshow admin endpoints are intercepted in the browser so the test
does not depend on a real /media share behind the E2E server. The mocked
/dirs endpoint models a small tree so the navigable browser can be
exercised: descending into a subfolder, adding it, going back, and adding a
second subfolder.
"""

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

# A tiny fake /media tree the mocked /dirs endpoint navigates.
_TREE = {
    "/media": ["Photos", "diskstation"],
    "/media/Photos": ["Urlaub", "Freunde"],
    "/media/Photos/Urlaub": [],
    "/media/Photos/Freunde": [],
    "/media/diskstation": [],
}


def _route_slideshow(page: Page) -> None:
    state = {"dirs": [], "photo_count": 0}

    def handle_get(route: Route) -> None:
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_put(route: Route) -> None:
        import json as _json

        body = _json.loads(route.request.post_data or "{}")
        state["dirs"] = body.get("dirs", [])
        state["photo_count"] = 7 * len(state["dirs"])
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_rescan(route: Route) -> None:
        state["photo_count"] = 12
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_dirs(route: Route) -> None:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(route.request.url).query)
        path = (query.get("path") or [""])[0] or "/media"
        children = _TREE.get(path, [])
        parent = None if path == "/media" else path.rsplit("/", 1)[0]
        route.fulfill(
            json={
                "media_root": "/media",
                "base": path,
                "parent": parent,
                "dirs": [
                    {"name": name, "path": f"{path}/{name}"} for name in children
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


def test_navigate_add_and_rescan(page: Page, server_url: str) -> None:
    _route_slideshow(page)
    _goto_admin(page, server_url)

    # Initially empty index and no configured directories.
    expect(page.locator("#slideshow-count")).to_have_text("0")
    expect(page.locator("#slideshow-dir-list")).to_contain_text("Noch keine Ordner")

    # The browser starts at the media root, showing its subfolders.
    browse = page.locator("#slideshow-browse-list .slideshow-browse-enter")
    expect(browse).to_have_count(2)
    expect(browse.first).to_contain_text("Photos")

    # Descend into Photos → its two subfolders appear; breadcrumb updates.
    page.locator(".slideshow-browse-enter", has_text="Photos").click()
    expect(browse).to_have_count(2)
    expect(page.locator("#slideshow-breadcrumb")).to_contain_text("Photos")
    expect(page.locator("#slideshow-breadcrumb .current")).to_have_text("Photos")

    # Descend into Urlaub (a leaf: no subfolders) and add it.
    page.locator(".slideshow-browse-enter", has_text="Urlaub").click()
    expect(page.locator("#slideshow-browse-list")).to_contain_text(
        "Keine Unterordner"
    )
    expect(page.locator("#slideshow-breadcrumb .current")).to_have_text("Urlaub")
    page.locator("#btn-slideshow-add").click()
    expect(page.locator("#slideshow-count")).to_have_text("7")
    expect(page.locator("#slideshow-dir-list .slideshow-dir")).to_have_count(1)
    expect(page.locator("#slideshow-dir-list .slideshow-dir-name")).to_have_text(
        "Urlaub"
    )
    # The add button now reports the current folder is already selected.
    expect(page.locator("#btn-slideshow-add")).to_be_disabled()

    # Go back up to Photos via the breadcrumb, then add a second subfolder.
    page.locator("#slideshow-breadcrumb .slideshow-crumb", has_text="Photos").click()
    expect(page.locator("#slideshow-breadcrumb .current")).to_have_text("Photos")
    page.locator(".slideshow-browse-enter", has_text="Freunde").click()
    expect(page.locator("#btn-slideshow-add")).to_be_enabled()
    page.locator("#btn-slideshow-add").click()
    expect(page.locator("#slideshow-dir-list .slideshow-dir")).to_have_count(2)
    expect(page.locator("#slideshow-count")).to_have_text("14")

    # Rescan reports a fresh count.
    page.locator("#btn-slideshow-rescan").click()
    expect(page.locator("#slideshow-count")).to_have_text("12")

    # Remove both directories again.
    page.locator("#slideshow-dir-list .action-button.subtle").first.click()
    expect(page.locator("#slideshow-dir-list .slideshow-dir")).to_have_count(1)
    page.locator("#slideshow-dir-list .action-button.subtle").first.click()
    expect(page.locator("#slideshow-dir-list")).to_contain_text("Noch keine Ordner")
