"""Browser E2E test for the admin "Diashow (Kiosk)" section.

The slideshow admin endpoints are intercepted in the browser so the test
does not depend on a real /media share behind the E2E server. The mocked
/dirs endpoint models a small tree so the navigable browser can be
exercised: descending into a subfolder, adding it, going back, and adding a
second subfolder.
"""

from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.e2e

FIXTURE_PNG = Path(__file__).resolve().parent.parent / "fixtures" / "slideshow-photo.png"

# A tiny fake /media tree the mocked /dirs endpoint navigates.
_TREE = {
    "/media": ["Photos", "diskstation"],
    "/media/Photos": ["Urlaub", "Freunde"],
    "/media/Photos/Urlaub": [],
    "/media/Photos/Freunde": [],
    "/media/diskstation": [],
}


def _route_slideshow(page: Page) -> None:
    state = {"dirs": [], "photo_count": 0, "videos": "off"}

    def handle_get(route: Route) -> None:
        route.fulfill(json={**state, "media_root": "/media"})

    def handle_put(route: Route) -> None:
        import json as _json

        body = _json.loads(route.request.post_data or "{}")
        state["dirs"] = body.get("dirs", [])
        state["photo_count"] = 7 * len(state["dirs"])
        # Mirrors the backend: an omitted "videos" leaves the value alone.
        if body.get("videos") is not None:
            state["videos"] = body["videos"]
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


def test_video_switch_saves_and_survives_a_reload(page: Page, server_url: str) -> None:
    """Etappe 33: videos are always indexed, so this switch only governs
    playback — it saves on its own and must come back from the server."""
    _route_slideshow(page)
    _goto_admin(page, server_url)

    select = page.locator("#slideshow-videos")
    expect(select).to_have_value("off")  # conservative default
    select.select_option("on")
    page.locator("#btn-slideshow-videos").click()
    expect(page.locator("#slideshow-message")).to_contain_text("Gespeichert")

    page.reload()
    expect(page.locator("#slideshow-videos")).to_have_value("on")


def test_adding_a_folder_leaves_the_video_switch_alone(
    page: Page, server_url: str
) -> None:
    """The folder buttons must not silently reset the video setting: they
    save without a "videos" field, which the backend treats as unchanged."""
    _route_slideshow(page)
    _goto_admin(page, server_url)

    page.locator("#slideshow-videos").select_option("on")
    page.locator("#btn-slideshow-videos").click()
    expect(page.locator("#slideshow-videos")).to_have_value("on")

    page.locator(".slideshow-browse-enter", has_text="Photos").click()
    page.locator(".slideshow-browse-enter", has_text="Urlaub").click()
    page.locator("#btn-slideshow-add").click()
    expect(page.locator("#slideshow-dir-list .slideshow-dir")).to_have_count(1)
    expect(page.locator("#slideshow-videos")).to_have_value("on")


# -- photos flagged for deletion (Etappe 49) ---------------------------------
#
# The kiosk's bin icon only sets a flag; this section is where the deletion
# becomes irreversible. What matters here: the path Roland asked to see, the
# two-tap confirmation, and that a FAILED deletion leaves the entry standing
# (the file is still on the NAS — a row that quietly vanished would pretend
# otherwise).

PENDING_ITEM = {
    "id": 42,
    "name": "urlaub.jpg",
    "path": "Photos/2019/Urlaub/urlaub.jpg",
    "folders": ["Photos", "2019", "Urlaub"],
    "taken": {"year": 2019, "month": 8, "day": 16},
    "kind": "image",
    "marked_at": "2026-10-09T18:30:00+00:00",
}
SECOND_ITEM = {**PENDING_ITEM, "id": 43, "name": "berge.jpg", "path": "Photos/berge.jpg"}


def _route_pending_deletes(
    page: Page, items: list[dict], *, delete_fails: bool = False
) -> dict:
    """Mock the three pending-delete endpoints plus the preview images."""
    state = {"items": list(items), "deleted": [], "withdrawn": []}
    png = FIXTURE_PNG.read_bytes()

    def handle_list(route: Route) -> None:
        route.fulfill(json={"items": state["items"]})

    def handle_delete(route: Route) -> None:
        photo_id = int(route.request.url.split("/")[-2])
        if delete_fails:
            route.fulfill(
                status=502,
                json={"detail": "Home Assistant hat das Löschen nicht bestätigt."},
            )
            return
        state["deleted"].append(photo_id)
        state["items"] = [item for item in state["items"] if item["id"] != photo_id]
        route.fulfill(json={"id": photo_id, "deleted": True, "path": "x"})

    def handle_withdraw(route: Route) -> None:
        photo_id = int(route.request.url.split("/")[-1])
        state["withdrawn"].append(photo_id)
        state["items"] = [item for item in state["items"] if item["id"] != photo_id]
        route.fulfill(json={"id": photo_id, "delete_requested": False})

    page.route("**/api/admin/slideshow/pending-deletes", handle_list)
    # A single "*" does not cross a slash, so this never catches /{id}/delete.
    page.route("**/api/admin/slideshow/pending-deletes/*", handle_withdraw)
    page.route("**/api/admin/slideshow/pending-deletes/*/delete", handle_delete)
    page.route(
        "**/api/slideshow/image/**",
        lambda route: route.fulfill(body=png, content_type="image/png"),
    )
    return state


def test_pending_deletes_list_shows_preview_name_and_path(
    page: Page, server_url: str
) -> None:
    _route_slideshow(page)
    _route_pending_deletes(page, [PENDING_ITEM, SECOND_ITEM])
    _goto_admin(page, server_url)

    entries = page.locator("#pending-delete-list .pending-delete-item")
    expect(entries).to_have_count(2)
    first = entries.first
    expect(first.locator(".pending-delete-name")).to_have_text("urlaub.jpg")
    # The file path is what Roland asked for explicitly.
    expect(first.locator(".pending-delete-path")).to_have_text(
        "Photos/2019/Urlaub/urlaub.jpg"
    )
    expect(first.locator(".pending-delete-marked")).to_contain_text("Vorgemerkt am")

    preview = first.locator(".pending-delete-preview")
    expect(preview).to_be_visible()
    assert "api/slideshow/image/42" in (preview.get_attribute("src") or "")
    # Bounded, so a portrait photo cannot push the page apart.
    box = preview.bounding_box()
    assert box is not None and box["height"] <= 140, box


def test_deleting_a_photo_takes_two_taps(page: Page, server_url: str) -> None:
    _route_slideshow(page)
    state = _route_pending_deletes(page, [PENDING_ITEM, SECOND_ITEM])
    _goto_admin(page, server_url)

    entry = page.locator("#pending-delete-list .pending-delete-item").first
    button = entry.locator(".action-button", has_text="Endgültig löschen")
    button.click()
    # First tap only arms it — nothing has been sent yet.
    expect(entry.locator(".action-button").first).to_contain_text("erneut tippen")
    assert state["deleted"] == []

    entry.locator(".action-button").first.click()
    expect(page.locator("#pending-delete-list .pending-delete-item")).to_have_count(1)
    assert state["deleted"] == [42]
    expect(page.locator(".pending-delete-name")).to_have_text("berge.jpg")


def test_keeping_a_photo_withdraws_the_request(page: Page, server_url: str) -> None:
    _route_slideshow(page)
    state = _route_pending_deletes(page, [PENDING_ITEM])
    _goto_admin(page, server_url)

    page.locator(".action-button", has_text="Doch behalten").click()
    expect(page.locator("#pending-delete-list")).to_contain_text(
        "Keine Fotos zum Löschen vorgemerkt"
    )
    assert state["withdrawn"] == [42]
    assert state["deleted"] == []


def test_a_failed_deletion_leaves_the_entry_standing(
    page: Page, server_url: str
) -> None:
    """502 = Home Assistant did not confirm the unlink. The file is still on
    the share, so the row must stay — with the reason next to it."""
    _route_slideshow(page)
    _route_pending_deletes(page, [PENDING_ITEM], delete_fails=True)
    _goto_admin(page, server_url)

    entry = page.locator("#pending-delete-list .pending-delete-item").first
    entry.locator(".action-button").first.click()  # arm
    entry.locator(".action-button").first.click()  # confirm -> 502

    expect(entry.locator(".message")).to_contain_text("nicht bestätigt")
    expect(page.locator("#pending-delete-list .pending-delete-item")).to_have_count(1)
    expect(entry.locator(".pending-delete-name")).to_have_text("urlaub.jpg")
    # Disarmed again, so a stray tap cannot retry it unnoticed.
    expect(entry.locator(".action-button").first).to_have_text("Endgültig löschen")
