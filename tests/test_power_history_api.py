"""Tests for the power history API (/api/power/history) with a mocked HA."""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import power
from app.main import app

PRODUCTION = "sensor.hoymiles_station_balkonkraftwerk_current_power"
CONSUMPTION = "sensor.stromverbrauch_gesamt"


def _series(entity_id: str, points: list[tuple[str, str]]) -> list[dict]:
    """One HA history series: list of {state, last_updated} for an entity."""
    return [
        {"entity_id": entity_id, "state": state, "last_updated": ts} for ts, state in points
    ]


class MockHistoryHA:
    """Mocked HA Core API history/period endpoint with a request counter."""

    def __init__(
        self,
        series: dict[str, list[dict]],
        *,
        down: bool = False,
        status: int = 200,
    ) -> None:
        self.series = series
        self.down = down
        self.status = status
        self.requests = 0
        self.last_url: httpx.URL | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.last_url = request.url
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "/api/history/period/" in request.url.path
        if self.down:
            raise httpx.ConnectError("connection refused")
        if self.status != 200:
            return httpx.Response(self.status)
        # HA returns a list of series in filter_entity_id order.
        filter_ids = request.url.params.get("filter_entity_id", "").split(",")
        body = [self.series.get(entity_id, []) for entity_id in filter_ids]
        return httpx.Response(200, json=body)

    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        def create_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler),
                base_url="http://supervisor/core/api",
                headers={"Authorization": "Bearer test-token"},
            )

        return create_client


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    power.reset_history_cache()
    return TestClient(app, client=("127.0.0.1", 50000))


def use_mock(monkeypatch: pytest.MonkeyPatch, mock: MockHistoryHA) -> None:
    monkeypatch.setattr(power, "create_client", mock.client_factory())


def _iso(minutes_ago: int, now: dt.datetime | None = None) -> str:
    base = now or dt.datetime(2026, 7, 6, 12, 0, tzinfo=dt.UTC)
    return (base - dt.timedelta(minutes=minutes_ago)).isoformat()


class TestHistoryEndpoint:
    def test_happy_path_returns_two_series(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(PRODUCTION, [(_iso(120), "100"), (_iso(60), "200")]),
                CONSUMPTION: _series(CONSUMPTION, [(_iso(120), "300"), (_iso(60), "400")]),
            }
        )
        use_mock(monkeypatch, mock)
        response = client.get("/api/power/history?hours=24")
        assert response.status_code == 200
        payload = response.json()
        assert payload["hours"] == 24
        assert [p["v"] for p in payload["production"]] == [100.0, 200.0]
        assert [p["v"] for p in payload["consumption"]] == [300.0, 400.0]
        # Each point carries a timestamp.
        assert all("t" in p for p in payload["production"])

    def test_non_numeric_and_unavailable_points_are_skipped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(
                    PRODUCTION,
                    [
                        (_iso(120), "unavailable"),
                        (_iso(90), "100"),
                        (_iso(60), "unknown"),
                        (_iso(30), "200"),
                    ],
                ),
                CONSUMPTION: _series(CONSUMPTION, [(_iso(60), "quatsch")]),
            }
        )
        use_mock(monkeypatch, mock)
        payload = client.get("/api/power/history?hours=24").json()
        assert [p["v"] for p in payload["production"]] == [100.0, 200.0]
        assert payload["consumption"] == []

    def test_downsampling_caps_points_per_series(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        many = [(_iso(10000 - i), str(i)) for i in range(5000)]
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(PRODUCTION, many),
                CONSUMPTION: _series(CONSUMPTION, many),
            }
        )
        use_mock(monkeypatch, mock)
        payload = client.get("/api/power/history?hours=168").json()
        assert 0 < len(payload["production"]) <= power.MAX_HISTORY_POINTS
        assert 0 < len(payload["consumption"]) <= power.MAX_HISTORY_POINTS

    def test_hours_param_is_validated_and_clamped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA({PRODUCTION: [], CONSUMPTION: []})
        use_mock(monkeypatch, mock)
        # An unsupported value falls back to the default (24).
        assert client.get("/api/power/history?hours=999").json()["hours"] == 24
        assert client.get("/api/power/history?hours=abc").json()["hours"] == 24
        assert client.get("/api/power/history").json()["hours"] == 24
        # Supported values pass through.
        assert client.get("/api/power/history?hours=72").json()["hours"] == 72
        assert client.get("/api/power/history?hours=168").json()["hours"] == 168

    def test_requests_the_two_fixed_aggregate_sensors_only(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA({PRODUCTION: [], CONSUMPTION: []})
        use_mock(monkeypatch, mock)
        client.get("/api/power/history?hours=24")
        entities = mock.last_url.params.get("filter_entity_id", "")
        assert set(entities.split(",")) == {PRODUCTION, CONSUMPTION}

    def test_ha_down_is_502_with_german_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_mock(monkeypatch, MockHistoryHA({}, down=True))
        response = client.get("/api/power/history?hours=24")
        assert response.status_code == 502
        assert response.json()["detail"] == "Home Assistant ist nicht erreichbar."

    def test_ha_error_status_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_mock(monkeypatch, MockHistoryHA({}, status=500))
        response = client.get("/api/power/history?hours=24")
        assert response.status_code == 502
        assert "Home Assistant" in response.json()["detail"]

    def test_empty_history_is_ok_with_empty_series(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_mock(monkeypatch, MockHistoryHA({PRODUCTION: [], CONSUMPTION: []}))
        payload = client.get("/api/power/history?hours=24").json()
        assert payload["production"] == []
        assert payload["consumption"] == []


class TestHistoryCache:
    def test_second_request_within_ttl_hits_the_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(PRODUCTION, [(_iso(60), "100")]),
                CONSUMPTION: _series(CONSUMPTION, [(_iso(60), "300")]),
            }
        )
        use_mock(monkeypatch, mock)
        first = client.get("/api/power/history?hours=24").json()
        assert mock.requests == 1
        second = client.get("/api/power/history?hours=24").json()
        assert mock.requests == 1
        assert second == first

    def test_cache_is_keyed_by_hours(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(PRODUCTION, [(_iso(60), "100")]),
                CONSUMPTION: _series(CONSUMPTION, [(_iso(60), "300")]),
            }
        )
        use_mock(monkeypatch, mock)
        client.get("/api/power/history?hours=24")
        client.get("/api/power/history?hours=72")
        assert mock.requests == 2  # different windows → separate fetches

    def test_cache_expires_after_ttl(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHistoryHA(
            {
                PRODUCTION: _series(PRODUCTION, [(_iso(60), "100")]),
                CONSUMPTION: _series(CONSUMPTION, [(_iso(60), "300")]),
            }
        )
        use_mock(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(power, "_now", lambda: clock["now"])
        client.get("/api/power/history?hours=24")
        assert mock.requests == 1
        clock["now"] += power.HISTORY_CACHE_TTL_SECONDS + 0.1
        client.get("/api/power/history?hours=24")
        assert mock.requests == 2


class TestDownsample:
    def test_downsample_keeps_short_series_untouched(self) -> None:
        points = [{"t": i, "v": float(i)} for i in range(10)]
        assert power.downsample(points, 300) == points

    def test_downsample_reduces_and_preserves_order(self) -> None:
        points = [{"t": i, "v": float(i)} for i in range(1000)]
        reduced = power.downsample(points, 100)
        assert len(reduced) <= 100
        times = [p["t"] for p in reduced]
        assert times == sorted(times)
