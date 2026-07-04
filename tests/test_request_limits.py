"""Tests for the global request-body size limit middleware."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import MAX_REQUEST_BODY_BYTES, app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def _chunked_put_scope(path: str) -> dict:
    """An ASGI scope for a PUT request with no Content-Length header.

    Mirrors what a chunked-transfer-encoding request looks like from the
    app's perspective: the body arrives as several http.request messages
    and there is no way to know the total size up front.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


class ChunkedBody:
    """Feeds a body to an ASGI app as several http.request chunks, no
    Content-Length — then keeps returning http.disconnect so a middleware
    bug that ignores more_body/keeps awaiting receive() cannot hang."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def __call__(self) -> dict:
        if self._chunks:
            chunk = self._chunks.pop(0)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": bool(self._chunks),
            }
        return {"type": "http.disconnect"}


async def _run_asgi_request(scope: dict, receive) -> tuple[int, bytes]:
    """Drive the ASGI app directly and collect the response status/body."""
    status_holder: dict = {}
    body_parts: list[bytes] = []

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    return status_holder["status"], b"".join(body_parts)


class TestRequestBodyLimit:
    def test_oversized_body_is_rejected_with_413(self, client: TestClient) -> None:
        payload = {"emojis": ["x" * (MAX_REQUEST_BODY_BYTES + 1)]}
        response = client.put("/api/tags/2026-07-10", json=payload)
        assert response.status_code == 413
        assert response.json()["detail"] == "Anfrage zu groß."

    def test_oversized_admin_put_is_rejected(self, client: TestClient) -> None:
        devices = [{"entity_id": "sensor.a_leistung", "name": "y" * 4000} for _ in range(10)]
        response = client.put("/api/admin/settings/power", json={"devices": devices})
        assert response.status_code == 413

    def test_normal_sized_requests_pass_through(self, client: TestClient) -> None:
        response = client.put("/api/tags/2026-07-10", json={"emojis": ["😀"]})
        assert response.status_code == 200

    def test_get_requests_are_unaffected(self, client: TestClient) -> None:
        assert client.get("/api/health").status_code == 200

    def test_body_at_the_limit_is_not_rejected_by_the_middleware(
        self, client: TestClient
    ) -> None:
        # Exactly at the limit: passes the middleware (the endpoint itself
        # may still reject the content — but not with 413).
        filler = "x" * (MAX_REQUEST_BODY_BYTES - 100)
        response = client.put("/api/tags/2026-07-10", json={"emojis": [filler]})
        assert response.status_code != 413


@pytest.mark.anyio
class TestChunkedBodyWithoutContentLength:
    """The truncation path in RequestBodyLimitMiddleware: bodies with no
    declared Content-Length (chunked transfer encoding) can only be capped
    while streaming, by cutting off once MAX_REQUEST_BODY_BYTES is exceeded.
    TestClient/httpx always sends a Content-Length, so this path needs a
    request driven directly at the ASGI level (no httpx involved)."""

    async def test_oversized_chunked_body_is_cut_off_not_hung(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        # One chunk already over the limit, plus a second chunk that must
        # never be needed/awaited if the middleware truncates correctly.
        oversized = json.dumps({"emojis": ["x" * (MAX_REQUEST_BODY_BYTES + 1)]}).encode()
        receive = ChunkedBody([oversized, b"more-data-that-should-be-unreachable"])
        scope = _chunked_put_scope("/api/tags/2026-07-10")
        status, body = await asyncio.wait_for(_run_asgi_request(scope, receive), timeout=5)
        # Truncated body fails JSON parsing in the endpoint -> 422, not a
        # 200/413 misread and above all not a hang (the timeout would fail
        # the test if the middleware kept awaiting receive() forever).
        assert status == 422
        assert body  # a real (JSON) error body, not an empty response

    async def test_normal_sized_chunked_body_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        payload = json.dumps({"emojis": ["😀"]}).encode()
        # Split into several small chunks, as a real chunked request would.
        chunks = [payload[i : i + 4] for i in range(0, len(payload), 4)]
        receive = ChunkedBody(chunks)
        scope = _chunked_put_scope("/api/tags/2026-07-10")
        status, _body = await asyncio.wait_for(_run_asgi_request(scope, receive), timeout=5)
        assert status == 200
