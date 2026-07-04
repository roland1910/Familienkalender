"""Tests for the day-tags API (/api/tags)."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import MAX_TAGS_PER_DAY, TAG_OPTIONS


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


class TestTagOptionsEndpoint:
    def test_returns_the_full_catalog(self, client: TestClient) -> None:
        response = client.get("/api/tags/options")
        assert response.status_code == 200
        options = response.json()["options"]
        assert options == [
            {"id": option.id, "emoji": option.emoji} for option in TAG_OPTIONS
        ]

    def test_exposes_the_per_day_cap(self, client: TestClient) -> None:
        response = client.get("/api/tags/options")
        assert response.json()["max_per_day"] == MAX_TAGS_PER_DAY


class TestGetTags:
    def test_empty_without_any_tags(self, client: TestClient) -> None:
        response = client.get("/api/tags", params={"from": "2026-07-01", "to": "2026-07-31"})
        assert response.status_code == 200
        assert response.json() == {"tags": {}}

    def test_returns_tags_inside_the_range(self, client: TestClient) -> None:
        client.put("/api/tags/2026-07-10", json={"emojis": ["😀", "⭐"]})
        client.put("/api/tags/2026-08-01", json={"emojis": ["🎂"]})
        response = client.get("/api/tags", params={"from": "2026-07-01", "to": "2026-07-31"})
        assert response.json() == {"tags": {"2026-07-10": ["😀", "⭐"]}}

    def test_from_after_to_is_rejected_in_german(self, client: TestClient) -> None:
        response = client.get("/api/tags", params={"from": "2026-07-31", "to": "2026-07-01"})
        assert response.status_code == 400
        assert response.json()["detail"] == "'from' muss vor 'to' liegen"

    def test_invalid_date_is_rejected(self, client: TestClient) -> None:
        response = client.get("/api/tags", params={"from": "kaputt", "to": "2026-07-31"})
        assert response.status_code == 422


class TestPutTags:
    def test_sets_tags_and_returns_them(self, client: TestClient) -> None:
        response = client.put("/api/tags/2026-07-10", json={"emojis": ["😀", "⭐"]})
        assert response.status_code == 200
        assert response.json() == {"date": "2026-07-10", "emojis": ["😀", "⭐"]}

    def test_put_replaces_the_previous_tags(self, client: TestClient) -> None:
        client.put("/api/tags/2026-07-10", json={"emojis": ["😀", "⭐"]})
        client.put("/api/tags/2026-07-10", json={"emojis": ["🎉"]})
        response = client.get("/api/tags", params={"from": "2026-07-10", "to": "2026-07-10"})
        assert response.json() == {"tags": {"2026-07-10": ["🎉"]}}

    def test_empty_list_clears_the_day(self, client: TestClient) -> None:
        client.put("/api/tags/2026-07-10", json={"emojis": ["😀"]})
        client.put("/api/tags/2026-07-10", json={"emojis": []})
        response = client.get("/api/tags", params={"from": "2026-07-10", "to": "2026-07-10"})
        assert response.json() == {"tags": {}}

    def test_unknown_emoji_is_rejected_in_german(self, client: TestClient) -> None:
        response = client.put("/api/tags/2026-07-10", json={"emojis": ["💩"]})
        assert response.status_code == 400
        assert response.json()["detail"] == "Unbekanntes Symbol."

    def test_free_text_is_rejected(self, client: TestClient) -> None:
        response = client.put(
            "/api/tags/2026-07-10", json={"emojis": ["<img onerror=alert(1)>"]}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Unbekanntes Symbol."

    def test_too_many_tags_are_rejected_in_german(self, client: TestClient) -> None:
        emojis = [option.emoji for option in TAG_OPTIONS[: MAX_TAGS_PER_DAY + 1]]
        response = client.put("/api/tags/2026-07-10", json={"emojis": emojis})
        assert response.status_code == 400
        assert response.json()["detail"] == f"Höchstens {MAX_TAGS_PER_DAY} Symbole pro Tag."

    def test_duplicates_are_collapsed(self, client: TestClient) -> None:
        response = client.put("/api/tags/2026-07-10", json={"emojis": ["😀", "😀"]})
        assert response.status_code == 200
        assert response.json()["emojis"] == ["😀"]

    def test_invalid_date_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/tags/gestern", json={"emojis": ["😀"]})
        assert response.status_code == 422

    def test_date_too_far_in_the_past_is_rejected_in_german(self, client: TestClient) -> None:
        too_early = (date.today() - timedelta(days=2 * 365 + 10)).isoformat()
        response = client.put(f"/api/tags/{too_early}", json={"emojis": ["😀"]})
        assert response.status_code == 400
        assert response.json()["detail"] == "Datum außerhalb des zulässigen Bereichs."

    def test_date_too_far_in_the_future_is_rejected_in_german(self, client: TestClient) -> None:
        too_late = (date.today() + timedelta(days=10 * 365 + 10)).isoformat()
        response = client.put(f"/api/tags/{too_late}", json={"emojis": ["😀"]})
        assert response.status_code == 400
        assert response.json()["detail"] == "Datum außerhalb des zulässigen Bereichs."

    def test_date_just_inside_the_window_is_accepted(self, client: TestClient) -> None:
        near_past = (date.today() - timedelta(days=2 * 365 - 10)).isoformat()
        near_future = (date.today() + timedelta(days=10 * 365 - 10)).isoformat()
        assert client.put(f"/api/tags/{near_past}", json={"emojis": []}).status_code == 200
        assert client.put(f"/api/tags/{near_future}", json={"emojis": []}).status_code == 200

    def test_more_than_32_emojis_is_rejected_before_dedup(self, client: TestClient) -> None:
        # Fail-fast payload cap (Pydantic Field max_length), independent of
        # the storage-layer dedup/cap check — protects against oversized
        # request bodies before any processing happens.
        response = client.put(
            "/api/tags/2026-07-10", json={"emojis": ["😀"] * 33}
        )
        assert response.status_code == 422
