"""Cache-Control headers for the app's own delivery and the JSON API.

Without an explicit Cache-Control the browser falls back to heuristic
caching, so after an add-on update kiosk/ingress clients keep running
stale JS (a hard reload does not reach the ingress iframe). ``no-cache``
forces revalidation on every request while conditional requests
(ETag/Last-Modified -> 304) keep delivery fast. The JSON API instead is
``no-store`` (see TestApiNoStore) — a heuristically cached /api/config
bit us live on the kiosk.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, client=("127.0.0.1", 50000))


@pytest.mark.parametrize(
    "path",
    [
        "/static/js/main.js",
        "/static/css/calendar.css",
        "/static/admin/main.js",
        "/",
        "/admin",
    ],
)
def test_app_delivery_forces_revalidation(path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_static_conditional_request_still_revalidates_via_304() -> None:
    """no-cache must keep 304 revalidation working (that is what makes it fast)."""
    first = client.get("/static/js/main.js")
    etag = first.headers["etag"]
    second = client.get("/static/js/main.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.headers["cache-control"] == "no-cache"


class TestApiNoStore:
    """JSON API responses must never be cached (Etappe 30).

    Live bug: after the v0.26.0 deploy the kiosk browser answered
    /api/config from its heuristic cache — an OLD payload without
    ``screensaver_default`` — so the server default never reached the
    device. API payloads are small and dynamic, so ``no-store`` (no
    revalidation dance needed) is the right policy. Exception: the
    slideshow image endpoint keeps its own ``private, max-age=60``
    (asserted positively in tests/test_slideshow.py).
    """

    @pytest.fixture
    def api_client(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
        # /api/config, /api/events etc. touch storage — keep it in tmp_path.
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        return TestClient(app, client=("127.0.0.1", 50000))

    @pytest.mark.parametrize("path", ["/api/config", "/api/sources", "/api/health"])
    def test_json_api_is_no_store(self, api_client: TestClient, path: str) -> None:
        response = api_client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def test_events_endpoint_is_no_store(self, api_client: TestClient) -> None:
        response = api_client.get("/api/events", params={"from": "2026-07-01", "to": "2026-07-31"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def test_error_responses_are_no_store_too(self, api_client: TestClient) -> None:
        # A cached error would be just as sticky as a cached old payload.
        response = api_client.get("/api/events", params={"from": "2026-07-31", "to": "2026-07-01"})
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"

    def test_api_no_store_applies_behind_ingress_prefix(self, api_client: TestClient) -> None:
        # Behind HA ingress the middleware sees the re-prefixed path
        # (/api/hassio_ingress/<token>/api/...); the no-store decision must
        # strip the root_path first, not match on the raw path.
        response = api_client.get(
            "/api/config", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    def test_slideshow_image_path_is_exempt(self, api_client: TestClient) -> None:
        # The image endpoint manages its own caching (private, max-age=60,
        # see tests/test_slideshow.py) — the API middleware must not stamp
        # no-store onto that path, not even onto its 404s.
        response = api_client.get("/api/slideshow/image/999999")
        assert response.status_code == 404
        assert response.headers.get("cache-control") != "no-store"

    def test_app_delivery_keeps_revalidation_policy(self, api_client: TestClient) -> None:
        # no-store is API-only; static assets/HTML stay on no-cache + 304.
        response = api_client.get("/static/js/main.js")
        assert response.headers["cache-control"] == "no-cache"
