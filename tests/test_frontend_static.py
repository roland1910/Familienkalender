"""Static guards for the frontend source.

Rule 4 (CLAUDE.md): event titles/locations come from foreign calendars and
must only ever be rendered via textContent — never through HTML-injection
sinks. This is additionally verified end-to-end in tests/e2e/.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

STATIC_DIR = Path(__file__).parent.parent / "app" / "static"

HTML_INJECTION_SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "DOMParser",
    "createContextualFragment",
    "srcdoc",
    "eval(",
    "new Function",
    "javascript:",
)

# Any inline event handler attribute (onclick=, onerror=, onpointerdown=, ...).
INLINE_HANDLER_PATTERN = re.compile(r"\bon\w+\s*=", re.IGNORECASE)

client = TestClient(app, client=("127.0.0.1", 50000))


def test_js_modules_exist() -> None:
    js_files = {path.name for path in (STATIC_DIR / "js").glob("*.js")}
    assert "main.js" in js_files
    assert "api.js" in js_files
    assert "month-view.js" in js_files
    assert "week-view.js" in js_files
    assert "gestures.js" in js_files


def test_js_never_uses_html_injection_sinks() -> None:
    # All JS below app/static — the calendar modules and the admin modules.
    js_files = list(STATIC_DIR.glob("**/*.js"))
    assert len(js_files) > 11, "expected calendar and admin JS files"
    for path in js_files:
        content = path.read_text(encoding="utf-8")
        for sink in HTML_INJECTION_SINKS:
            assert sink not in content, f"{path.name} verwendet {sink}"


def test_config_fetch_bypasses_the_browser_cache() -> None:
    """Belt and braces to the server-side no-store (Etappe 30): the
    /api/config fetch itself asks the browser to skip its cache — the
    kiosk once served a stale cached /api/config after a deploy."""
    content = (STATIC_DIR / "js" / "api.js").read_text(encoding="utf-8")
    assert re.search(
        r"""fetch\(\s*["']api/config["']\s*,\s*\{\s*cache:\s*["']no-store["']\s*\}\s*\)""",
        content,
    ), "api.js: fetchConfig muss { cache: \"no-store\" } setzen"


def test_html_pages_have_no_inline_event_handlers() -> None:
    for page in STATIC_DIR.glob("**/*.html"):
        content = page.read_text(encoding="utf-8")
        match = INLINE_HANDLER_PATTERN.search(content)
        assert match is None, f"{page.name} enthält Inline-Handler: {match.group(0)!r}"


def test_admin_modules_exist_and_are_served() -> None:
    for module in ("main", "api", "dom", "wizard-shared", "sources",
                   "nextcloud-wizard", "google-wizard", "settings", "power-devices",
                   "feed"):
        assert (STATIC_DIR / "admin" / f"{module}.js").is_file()
        response = client.get(f"/static/admin/{module}.js")
        assert response.status_code == 200, f"admin/{module}.js not served"
        assert "javascript" in response.headers["content-type"]
    css = client.get("/static/css/admin.css")
    assert css.status_code == 200


def test_stylesheet_and_modules_are_served() -> None:
    css = client.get("/static/css/calendar.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    for module in ("main", "api", "state", "dates", "events", "colors",
                   "month-view", "week-view", "gestures", "popover", "dom",
                   "power-view", "power-format", "legend", "view-memory",
                   "theme-memory"):
        response = client.get(f"/static/js/{module}.js")
        assert response.status_code == 200, f"{module}.js not served"
        assert "javascript" in response.headers["content-type"]
