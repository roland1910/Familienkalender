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

client = TestClient(app, client=("127.0.0.1", 50000))


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


async def _get_root_path_for_header(header_value: str) -> str:
    """Run a request with the given X-Ingress-Path value, return the root_path."""
    captured: dict[str, str] = {}

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["root_path"] = scope.get("root_path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = IngressPathMiddleware(asgi_app)
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
        await async_client.get("/", headers={"X-Ingress-Path": header_value})
    return captured["root_path"]


@pytest.mark.anyio
async def test_valid_ingress_header_is_accepted() -> None:
    assert await _get_root_path_for_header(INGRESS_PATH) == INGRESS_PATH


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header_value",
    [
        "https://evil.example",
        "/other/path",
        "/api/hassio_ingress/token://evil.example",
        "/api/hassio_ingress/token\twith-tab",
        "/api/hassio_ingress/token with-space",
        "",
    ],
)
async def test_invalid_ingress_header_is_ignored(header_value: str) -> None:
    assert await _get_root_path_for_header(header_value) == ""


@pytest.mark.anyio
async def test_ingress_header_with_newline_is_rejected_or_ignored() -> None:
    """A header value containing a raw newline must never become root_path.

    httpx refuses to send such a header at all; if a proxy smuggles one in,
    the middleware must ignore it. Exercise the middleware scope directly.
    """
    captured: dict[str, str] = {}

    async def asgi_app(scope: Scope, receive: Receive, send: Send) -> None:
        captured["root_path"] = scope.get("root_path", "")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = IngressPathMiddleware(asgi_app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-ingress-path", b"/api/hassio_ingress/tok\nen")],
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await wrapped(scope, receive, send)
    assert captured["root_path"] == ""


def test_invalid_ingress_header_does_not_crash_the_app() -> None:
    response = client.get("/", headers={"X-Ingress-Path": "https://evil.example"})
    assert response.status_code == 200


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
