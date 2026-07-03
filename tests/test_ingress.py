"""Tests for Home Assistant ingress path handling.

HA ingress proxies requests to the add-on with the ingress prefix stripped
and passes the external base path in the X-Ingress-Path header. The app must
work both with and without that header and must expose the header value as
the ASGI root_path so URL generation stays correct.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from app.main import IngressPathMiddleware, app

INGRESS_PATH = "/api/hassio_ingress/abc123token"

client = TestClient(app)


def test_index_works_behind_ingress() -> None:
    response = client.get("/", headers={"X-Ingress-Path": INGRESS_PATH})
    assert response.status_code == 200
    assert "Familienkalender" in response.text


def test_health_works_behind_ingress() -> None:
    response = client.get("/api/health", headers={"X-Ingress-Path": INGRESS_PATH})
    assert response.status_code == 200


@pytest.mark.anyio
async def test_middleware_sets_root_path_from_header() -> None:
    captured: dict[str, str] = {}

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["root_path"] = scope.get("root_path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = IngressPathMiddleware(asgi_app)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        await async_client.get("/", headers={"X-Ingress-Path": INGRESS_PATH})
    assert captured["root_path"] == INGRESS_PATH


@pytest.mark.anyio
async def test_middleware_without_header_keeps_root_path_empty() -> None:
    captured: dict[str, str] = {}

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["root_path"] = scope.get("root_path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = IngressPathMiddleware(asgi_app)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        await async_client.get("/")
    assert captured["root_path"] == ""


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
