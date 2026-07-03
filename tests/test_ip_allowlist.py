"""Tests for the client IP allowlist.

The add-on must only be reachable through the HA ingress proxy
(172.30.32.2). Direct requests from any other client IP get a 403.
For tests and local development the allowlist is configurable via the
ALLOWED_CLIENT_IPS environment variable (default allows the ingress
proxy and 127.0.0.1 for local healthchecks).
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.main import ClientIPAllowlistMiddleware, app

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
