"""Static guards for the frontend source.

Rule 4 (CLAUDE.md): event titles/locations come from foreign calendars and
must only ever be rendered via textContent — never through HTML-injection
sinks. This is additionally verified end-to-end in tests/e2e/.
"""

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
)

client = TestClient(app, client=("127.0.0.1", 50000))


def test_js_modules_exist() -> None:
    js_files = {path.name for path in (STATIC_DIR / "js").glob("*.js")}
    assert "main.js" in js_files
    assert "api.js" in js_files
    assert "month-view.js" in js_files
    assert "week-view.js" in js_files
    assert "gestures.js" in js_files


def test_js_never_uses_html_injection_sinks() -> None:
    js_files = list((STATIC_DIR / "js").glob("**/*.js"))
    assert js_files, "no JS files found"
    for path in js_files:
        content = path.read_text(encoding="utf-8")
        for sink in HTML_INJECTION_SINKS:
            assert sink not in content, f"{path.name} verwendet {sink}"


def test_index_has_no_inline_event_handlers() -> None:
    content = (STATIC_DIR / "index.html").read_text(encoding="utf-8").lower()
    for attribute in ("onclick=", "onerror=", "onload="):
        assert attribute not in content


def test_stylesheet_and_modules_are_served() -> None:
    css = client.get("/static/css/calendar.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    for module in ("main", "api", "state", "dates", "events", "colors",
                   "month-view", "week-view", "gestures", "popover", "dom"):
        response = client.get(f"/static/js/{module}.js")
        assert response.status_code == 200, f"{module}.js not served"
        assert "javascript" in response.headers["content-type"]
