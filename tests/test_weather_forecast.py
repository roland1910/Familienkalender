"""Tests for the weather forecast API (/api/weather/forecast) with a mocked MET Norway.

MET Norway's terms of service require a descriptive User-Agent, caching and
conditional requests (If-Modified-Since); those obligations are asserted
here so a refactor cannot quietly drop them.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import weather
from app.main import app


def _entry(
    time_iso: str,
    *,
    temp: float | None = 20.0,
    wind: float | None = 3.0,
    wind_dir: float | None = 180.0,
    precip: float | None = 0.0,
) -> dict:
    """One MET Norway timeseries entry (fields omitted when None)."""
    instant: dict = {}
    if temp is not None:
        instant["air_temperature"] = temp
    if wind is not None:
        instant["wind_speed"] = wind
    if wind_dir is not None:
        instant["wind_from_direction"] = wind_dir
    data: dict = {"instant": {"details": instant}}
    if precip is not None:
        data["next_1_hours"] = {"details": {"precipitation_amount": precip}}
    return {"time": time_iso, "data": data}


def _forecast_body(entries: list[dict]) -> dict:
    return {
        "type": "Feature",
        "properties": {"meta": {"updated_at": "2026-07-21T10:00:00Z"}, "timeseries": entries},
    }


def _hours_ahead(count: int, start: dt.datetime | None = None) -> list[str]:
    base = start or dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return [(base + dt.timedelta(hours=i)).isoformat().replace("+00:00", "Z") for i in range(count)]


class MockMet:
    """Mocked MET Norway locationforecast endpoint with a request counter."""

    def __init__(
        self,
        body: dict | None = None,
        *,
        down: bool = False,
        status: int = 200,
        raw: bytes | None = None,
        last_modified: str | None = "Tue, 21 Jul 2026 10:00:00 GMT",
        expires: str | None = None,
    ) -> None:
        self.body = body if body is not None else _forecast_body([])
        self.down = down
        self.status = status
        self.raw = raw
        self.last_modified = last_modified
        self.expires = expires
        self.requests = 0
        self.last_request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.last_request = request
        if self.down:
            raise httpx.ConnectError("connection refused")
        headers = {}
        if self.last_modified:
            headers["Last-Modified"] = self.last_modified
        if self.expires:
            headers["Expires"] = self.expires
        # 203 ("deprecated product version") still carries a usable body.
        if self.status not in (200, 203):
            return httpx.Response(self.status, headers=headers)
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw, headers=headers)
        return httpx.Response(self.status, json=self.body, headers=headers)

    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        def create_met_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler),
                base_url=weather.MET_BASE_URL,
                headers={"User-Agent": weather.USER_AGENT},
            )

        return create_met_client


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    weather.reset_caches()
    return TestClient(app, client=("127.0.0.1", 50000))


def use_met(monkeypatch: pytest.MonkeyPatch, mock: MockMet) -> None:
    monkeypatch.setattr(weather, "create_met_client", mock.client_factory())


class TestForecastEndpoint:
    def test_happy_path_returns_hourly_points(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = _hours_ahead(4)
        mock = MockMet(
            _forecast_body(
                [
                    _entry(times[0], temp=18.5, wind=2.5, wind_dir=90.0, precip=0.0),
                    _entry(times[1], temp=19.5, wind=3.0, wind_dir=100.0, precip=0.4),
                    _entry(times[2], temp=21.0, wind=3.5, wind_dir=110.0, precip=1.2),
                    _entry(times[3], temp=20.0, wind=4.0, wind_dir=120.0, precip=0.0),
                ]
            )
        )
        use_met(monkeypatch, mock)
        response = client.get("/api/weather/forecast")
        assert response.status_code == 200
        payload = response.json()
        points = payload["points"]
        assert len(points) == 4
        assert [p["temp_c"] for p in points] == [18.5, 19.5, 21.0, 20.0]
        assert [p["precip_mm"] for p in points] == [0.0, 0.4, 1.2, 0.0]
        assert [p["wind_ms"] for p in points] == [2.5, 3.0, 3.5, 4.0]
        assert [p["wind_dir_deg"] for p in points] == [90.0, 100.0, 110.0, 120.0]
        # Timestamps are epoch milliseconds, ascending.
        assert all(isinstance(p["t"], int) for p in points)
        assert [p["t"] for p in points] == sorted(p["t"] for p in points)

    def test_passes_through_the_full_timeseries(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The frontend slices 24h/48h/96h client-side, so the backend must
        # hand through the whole series MET returns rather than truncate it
        # at 48h (Etappe 37: the 96h default needs points past 48h).
        times = _hours_ahead(150)
        use_met(monkeypatch, MockMet(_forecast_body([_entry(t) for t in times])))
        points = client.get("/api/weather/forecast").json()["points"]
        span_hours = (points[-1]["t"] - points[0]["t"]) / 3_600_000
        assert span_hours >= 96.0
        # All mocked hours come through (nothing dropped by the window).
        assert len(points) == 150

    def test_missing_fields_become_null_instead_of_failing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = _hours_ahead(3)
        mock = MockMet(
            _forecast_body(
                [
                    _entry(times[0], temp=None, precip=None),
                    _entry(times[1], wind=None, wind_dir=None),
                    # Entry with no data block at all.
                    {"time": times[2]},
                ]
            )
        )
        use_met(monkeypatch, mock)
        points = client.get("/api/weather/forecast").json()["points"]
        assert len(points) == 3
        assert points[0]["temp_c"] is None
        assert points[0]["precip_mm"] is None
        assert points[1]["wind_ms"] is None
        assert points[1]["wind_dir_deg"] is None
        assert points[2]["temp_c"] is None

    def test_non_numeric_values_are_dropped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = _hours_ahead(1)
        body = _forecast_body([_entry(times[0], temp=20.0)])
        details = body["properties"]["timeseries"][0]["data"]["instant"]["details"]
        details["air_temperature"] = "warm"
        use_met(monkeypatch, MockMet(body))
        points = client.get("/api/weather/forecast").json()["points"]
        assert points[0]["temp_c"] is None

    def test_entries_with_unparseable_time_are_skipped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = _hours_ahead(2)
        use_met(
            monkeypatch,
            MockMet(
                _forecast_body(
                    [_entry("not-a-time"), _entry(times[0]), {"time": None}, _entry(times[1])]
                )
            ),
        )
        assert len(client.get("/api/weather/forecast").json()["points"]) == 2

    def test_past_entries_are_dropped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        old = (now - dt.timedelta(hours=12)).isoformat().replace("+00:00", "Z")
        future = _hours_ahead(2, now)
        use_met(monkeypatch, MockMet(_forecast_body([_entry(old), *[_entry(t) for t in future]])))
        points = client.get("/api/weather/forecast").json()["points"]
        assert len(points) == 2

    def test_broken_json_is_502_with_german_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_met(monkeypatch, MockMet(raw=b"{not json"))
        response = client.get("/api/weather/forecast")
        assert response.status_code == 502
        assert response.json()["detail"] == "Der Wetterdienst liefert eine unlesbare Antwort."

    def test_unexpected_shape_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_met(monkeypatch, MockMet({"properties": {"timeseries": "nope"}}))
        response = client.get("/api/weather/forecast")
        assert response.status_code == 502
        assert "Wetterdienst" in response.json()["detail"]

    def test_service_down_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_met(monkeypatch, MockMet(down=True))
        response = client.get("/api/weather/forecast")
        assert response.status_code == 502
        assert response.json()["detail"] == "Der Wetterdienst ist nicht erreichbar."

    def test_error_status_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_met(monkeypatch, MockMet(status=503))
        response = client.get("/api/weather/forecast")
        assert response.status_code == 502
        assert "HTTP 503" in response.json()["detail"]

    def test_oversized_response_is_rejected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(weather, "MAX_FORECAST_RESPONSE_BYTES", 64)
        times = _hours_ahead(50)
        use_met(monkeypatch, MockMet(_forecast_body([_entry(t) for t in times])))
        response = client.get("/api/weather/forecast")
        assert response.status_code == 502
        assert "zu groß" in response.json()["detail"]


class TestMetTermsOfService:
    """MET Norway requires identification, caching and conditional requests."""

    def test_sends_descriptive_user_agent_and_munich_coordinates(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(2)]))
        use_met(monkeypatch, mock)
        client.get("/api/weather/forecast")
        request = mock.last_request
        assert request is not None
        agent = request.headers["User-Agent"]
        assert "Familienkalender" in agent
        # A contact (repository URL) is part of MET's requirement.
        assert "github.com/roland1910/Familienkalender" in agent
        assert request.url.params["lat"] == str(weather.MUNICH_LAT)
        assert request.url.params["lon"] == str(weather.MUNICH_LON)

    def test_second_request_within_ttl_is_served_from_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(3)]))
        use_met(monkeypatch, mock)
        first = client.get("/api/weather/forecast").json()
        assert mock.requests == 1
        second = client.get("/api/weather/forecast").json()
        assert mock.requests == 1
        assert second == first

    def test_cache_ttl_is_at_least_30_minutes(self) -> None:
        assert weather.FORECAST_CACHE_TTL_SECONDS >= 1800

    def test_refetch_after_ttl_sends_if_modified_since(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(3)]))
        use_met(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(weather, "_now", lambda: clock["now"])
        client.get("/api/weather/forecast")
        assert mock.last_request is not None
        assert "If-Modified-Since" not in mock.last_request.headers
        clock["now"] += weather.FORECAST_CACHE_TTL_SECONDS + 1
        client.get("/api/weather/forecast")
        assert mock.requests == 2
        assert mock.last_request.headers["If-Modified-Since"] == "Tue, 21 Jul 2026 10:00:00 GMT"

    def test_304_keeps_the_cached_payload(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(3)]))
        use_met(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(weather, "_now", lambda: clock["now"])
        first = client.get("/api/weather/forecast").json()
        clock["now"] += weather.FORECAST_CACHE_TTL_SECONDS + 1
        mock.status = 304
        second = client.get("/api/weather/forecast").json()
        assert second == first
        # ... and the refreshed TTL means no further request right away.
        third = client.get("/api/weather/forecast").json()
        assert third == first
        assert mock.requests == 2

    def test_203_is_treated_as_a_successful_response(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MET answers 203 for deprecated product versions; the body is valid.
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(2)]), status=203)
        use_met(monkeypatch, mock)
        response = client.get("/api/weather/forecast")
        assert response.status_code == 200
        assert len(response.json()["points"]) == 2

    def test_expires_header_extends_the_cache_lifetime(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        far_future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
        expires = far_future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        mock = MockMet(_forecast_body([_entry(t) for t in _hours_ahead(2)]), expires=expires)
        use_met(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(weather, "_now", lambda: clock["now"])
        client.get("/api/weather/forecast")
        # Still cached well past the minimum TTL because Expires says so.
        clock["now"] += weather.FORECAST_CACHE_TTL_SECONDS + 60
        client.get("/api/weather/forecast")
        assert mock.requests == 1

    def test_errors_are_cached_briefly_so_a_down_service_is_not_hammered(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMet(down=True)
        use_met(monkeypatch, mock)
        assert client.get("/api/weather/forecast").status_code == 502
        assert client.get("/api/weather/forecast").status_code == 502
        assert mock.requests == 1
