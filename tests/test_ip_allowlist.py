"""Tests for the client IP allowlist.

The add-on must only be reachable through the HA ingress proxy
(172.30.32.2). Direct requests from any other client IP get a 403 —
with exactly one exception: /feed/... is reachable from any IP, because
subscription clients on LAN phones cannot authenticate against HA; the
URL token is the sole auth there (see test_feed_route.py). For tests and
local development the allowlist is configurable via the
ALLOWED_CLIENT_IPS environment variable (default allows the ingress
proxy and 127.0.0.1 for local healthchecks).
"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.main import ClientIPAllowlistMiddleware, app
from app.settings import ensure_feed_token
from app.storage import Storage, default_db_path

HA_INGRESS_IP = "172.30.32.2"


def test_request_from_ingress_proxy_is_allowed() -> None:
    client = TestClient(app, client=(HA_INGRESS_IP, 50000))
    response = client.get("/api/health")
    assert response.status_code == 200


def test_request_from_localhost_is_allowed() -> None:
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/api/health")
    assert response.status_code == 200


def test_request_from_foreign_ip_is_rejected() -> None:
    client = TestClient(app, client=("192.168.1.50", 50000))
    response = client.get("/api/health")
    assert response.status_code == 403


def test_request_from_foreign_ip_is_rejected_for_static_files() -> None:
    client = TestClient(app, client=("10.0.0.5", 50000))
    response = client.get("/static/styles.css")
    assert response.status_code == 403


def test_request_from_foreign_ip_is_rejected_for_admin_api() -> None:
    # The admin endpoints manage credentials — they must sit behind the
    # same allowlist as everything else (no separate auth layer exists).
    client = TestClient(app, client=("192.168.1.77", 50000))
    assert client.get("/api/admin/settings").status_code == 403
    assert client.get("/api/admin/sources").status_code == 403
    assert (
        client.post("/api/admin/sources", json={"type": "caldav"}).status_code == 403
    )


class TestFeedExceptionForForeignIPs:
    """LAN clients reach only /feed/... — everything else stays 403.

    Security-critical: the feed path carries its own auth (URL token),
    every other route relies on the allowlist + HA ingress.
    """

    @pytest.fixture
    def storage(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return Storage(default_db_path())

    @pytest.fixture
    def lan_client(self, storage: Storage) -> TestClient:
        return TestClient(app, client=("192.168.1.42", 50000))

    def test_foreign_ip_reaches_the_feed_with_a_valid_token(
        self, lan_client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        response = lan_client.get(f"/feed/{token}.ics")
        assert response.status_code == 200
        assert "BEGIN:VCALENDAR" in response.text

    def test_foreign_ip_with_wrong_token_gets_404_not_content(
        self, lan_client: TestClient, storage: Storage
    ) -> None:
        ensure_feed_token(storage)
        response = lan_client.get("/feed/geraten.ics")
        assert response.status_code == 404
        assert "VCALENDAR" not in response.text

    @pytest.mark.parametrize(
        "path",
        [
            "/api/events?from=2026-07-01&to=2026-07-02",
            "/api/sources",
            "/api/health",
            "/api/me",
            "/api/admin/settings",
            "/api/admin/sources",
            "/admin",
            "/static/js/main.js",
            "/static/admin/main.js",
            "/",
            "/feed",  # only /feed/<token>.ics, not the bare prefix
            "/feedx/etwas.ics",  # prefix must not match sibling paths
        ],
    )
    def test_foreign_ip_stays_403_everywhere_else(
        self, lan_client: TestClient, storage: Storage, path: str
    ) -> None:
        ensure_feed_token(storage)
        assert lan_client.get(path).status_code == 403

    def test_foreign_ip_cannot_post_to_the_feed_namespace_endpoints(
        self, lan_client: TestClient, storage: Storage
    ) -> None:
        # Write endpoints stay blocked regardless of the feed exception.
        assert lan_client.post("/api/sync").status_code == 403
        assert lan_client.put("/api/admin/settings", json={}).status_code == 403

    def test_ingress_ip_keeps_full_access_including_the_feed(
        self, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        ingress = TestClient(app, client=(HA_INGRESS_IP, 50000))
        assert ingress.get("/api/health").status_code == 200
        assert ingress.get(f"/feed/{token}.ics").status_code == 200


@pytest.mark.anyio
async def test_allowlist_is_configurable_via_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_CLIENT_IPS", "10.1.2.3")

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = ClientIPAllowlistMiddleware(asgi_app)

    allowed = httpx.ASGITransport(app=wrapped, client=("10.1.2.3", 123))
    async with httpx.AsyncClient(transport=allowed, base_url="http://test") as async_client:
        response = await async_client.get("/")
    assert response.status_code == 200

    denied = httpx.ASGITransport(app=wrapped, client=("127.0.0.1", 123))
    async with httpx.AsyncClient(transport=denied, base_url="http://test") as async_client:
        response = await async_client.get("/")
    assert response.status_code == 403


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
