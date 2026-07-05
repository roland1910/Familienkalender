"""Tests for the client IP allowlist.

The main app must only be reachable through the HA ingress proxy
(172.30.32.2). Direct requests from any other client IP get a 403 —
without exceptions: the ICS feed has moved to its own listener
(app.feed_app, served by app.serve on a separate port), so the former
/feed allowlist exception is gone and feed paths are blocked here like
everything else. For tests and local development the allowlist is
configurable via the ALLOWED_CLIENT_IPS environment variable (default
allows the ingress proxy and 127.0.0.1 for local healthchecks).
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


class TestNoFeedExceptionAnymore:
    """The feed lives on its own listener now — no allowlist holes here.

    Security-critical: since Etappe 10 the main app is ingress-only
    again. Even a valid feed URL must be 403 on this app; subscription
    clients talk to the dedicated feed port instead (app.feed_app).
    """

    @pytest.fixture
    def storage(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return Storage(default_db_path())

    @pytest.fixture
    def lan_client(self, storage: Storage) -> TestClient:
        return TestClient(app, client=("192.168.1.42", 50000))

    def test_foreign_ip_gets_403_even_with_a_valid_feed_token(
        self, lan_client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        response = lan_client.get(f"/feed/{token}.ics")
        assert response.status_code == 403
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
            "/feed",
            "/feed/irgendwas.ics",
        ],
    )
    def test_foreign_ip_stays_403_everywhere(
        self, lan_client: TestClient, storage: Storage, path: str
    ) -> None:
        ensure_feed_token(storage)
        assert lan_client.get(path).status_code == 403

    def test_ingress_ip_keeps_full_access_but_the_feed_route_is_gone(
        self, storage: Storage
    ) -> None:
        # The feed route itself was removed from the main app: ingress
        # users get a plain 404 (nothing to fetch here), feed clients use
        # the dedicated listener.
        token = ensure_feed_token(storage)
        ingress = TestClient(app, client=(HA_INGRESS_IP, 50000))
        assert ingress.get("/api/health").status_code == 200
        assert ingress.get(f"/feed/{token}.ics").status_code == 404


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
