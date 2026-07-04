"""Tests for the global request-body size limit middleware."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import MAX_REQUEST_BODY_BYTES, app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


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
