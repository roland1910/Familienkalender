"""Cache-Control headers for the app's own delivery (static assets + HTML).

Without an explicit Cache-Control the browser falls back to heuristic
caching, so after an add-on update kiosk/ingress clients keep running
stale JS (a hard reload does not reach the ingress iframe). ``no-cache``
forces revalidation on every request while conditional requests
(ETag/Last-Modified -> 304) keep delivery fast.
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


def test_api_responses_are_not_forced_to_no_cache() -> None:
    """Only the app delivery is covered; API endpoints keep their own policy
    (e.g. the slideshow image endpoint sets private, max-age=60 itself)."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers.get("cache-control") != "no-cache"
