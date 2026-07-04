"""Tests for the power view API (/api/power) with mocked HA Core API."""

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import power
from app.main import app
from app.settings import PowerDevice, set_power_devices
from app.storage import Storage, default_db_path

# States the mocked HA instance reports (happy path).
HAPPY_STATES = {
    "sensor.hoymiles_station_balkonkraftwerk_current_power": "350.5",
    "sensor.stromverbrauch_gesamt": "487.2",
    "sensor.strom_bilanz": "-136.7",
    "sensor.strom_ueberschuss": "0.0",
    "sensor.strom_netzbezug": "136.7",
    "sensor.kuhlschrank_leistung": "45.3",
    "sensor.tv_sideboard_leistung": "12.0",
    "sensor.spuhlmaschiene_leistung": "0.0",
    "sensor.schreibtisch_leistung": "88.1",
    "sensor.matter_over_wifi_smart_plug_6_leistung": "unavailable",
}


class MockHA:
    """Mocked HA Core API: per-entity states plus a request counter."""

    def __init__(self, states: dict[str, str], *, down: bool = False) -> None:
        self.states = states
        self.down = down
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "/api/states/" in request.url.path
        if self.down:
            raise httpx.ConnectError("connection refused")
        entity_id = request.url.path.rsplit("/", 1)[-1]
        if entity_id not in self.states:
            return httpx.Response(404, json={"message": "Entity not found."})
        return httpx.Response(
            200, json={"entity_id": entity_id, "state": self.states[entity_id]}
        )

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
    power.reset_cache()
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


def use_mock_ha(monkeypatch: pytest.MonkeyPatch, mock: MockHA) -> None:
    monkeypatch.setattr(power, "create_client", mock.client_factory())


class TestPowerEndpoint:
    def test_happy_path_returns_all_metrics_and_devices(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_mock_ha(monkeypatch, MockHA(HAPPY_STATES))
        response = client.get("/api/power")
        assert response.status_code == 200
        payload = response.json()
        assert payload["production"] == {"value": 350.5, "available": True}
        assert payload["consumption"] == {"value": 487.2, "available": True}
        assert payload["balance"] == {"value": -136.7, "available": True}
        assert payload["surplus"] == {"value": 0.0, "available": True}
        assert payload["grid_import"] == {"value": 136.7, "available": True}
        devices = payload["devices"]
        assert [device["name"] for device in devices] == [
            "Kühlschrank", "TV-Sideboard", "Spülmaschine", "Schreibtisch", "Steckdose 6",
        ]
        assert devices[0] == {
            "entity_id": "sensor.kuhlschrank_leistung",
            "name": "Kühlschrank",
            "value": 45.3,
            "available": True,
        }

    def test_unavailable_state_is_zero_and_flagged(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = HAPPY_STATES | {"sensor.stromverbrauch_gesamt": "unavailable"}
        use_mock_ha(monkeypatch, MockHA(states))
        payload = client.get("/api/power").json()
        assert payload["consumption"] == {"value": 0.0, "available": False}
        # The last default device reports "unavailable" in the fixture.
        assert payload["devices"][-1]["value"] == 0.0
        assert payload["devices"][-1]["available"] is False

    def test_unknown_and_non_numeric_states_are_flagged(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = HAPPY_STATES | {
            "sensor.strom_bilanz": "unknown",
            "sensor.strom_ueberschuss": "quatsch",
        }
        use_mock_ha(monkeypatch, MockHA(states))
        payload = client.get("/api/power").json()
        assert payload["balance"] == {"value": 0.0, "available": False}
        assert payload["surplus"] == {"value": 0.0, "available": False}

    def test_ha_down_is_502_with_german_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_mock_ha(monkeypatch, MockHA({}, down=True))
        response = client.get("/api/power")
        assert response.status_code == 502
        assert response.json()["detail"] == "Home Assistant ist nicht erreichbar."

    def test_missing_entity_is_502_naming_the_sensor(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = dict(HAPPY_STATES)
        del states["sensor.strom_netzbezug"]
        use_mock_ha(monkeypatch, MockHA(states))
        response = client.get("/api/power")
        assert response.status_code == 502
        assert "sensor.strom_netzbezug" in response.json()["detail"]
        assert "unbekannt" in response.json()["detail"]

    def test_first_of_several_simultaneous_errors_wins_by_list_order(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two different failure kinds at once (a missing sensor plus a
        # non-2xx HA error) to document that "first error" in
        # _fetch_snapshot_uncached means first in entity_ids/results order,
        # not most severe — the production sensor is first in
        # AGGREGATE_ENTITIES, so its "unbekannt" error wins over the 500
        # from a later sensor, regardless of which one is worse.
        states = dict(HAPPY_STATES)
        del states["sensor.hoymiles_station_balkonkraftwerk_current_power"]

        def handler(request: httpx.Request) -> httpx.Response:
            entity_id = request.url.path.rsplit("/", 1)[-1]
            if entity_id == "sensor.stromverbrauch_gesamt":
                return httpx.Response(500)
            if entity_id not in states:
                return httpx.Response(404, json={"message": "Entity not found."})
            return httpx.Response(200, json={"entity_id": entity_id, "state": states[entity_id]})

        def create_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://supervisor/core/api",
                headers={"Authorization": "Bearer test-token"},
            )

        monkeypatch.setattr(power, "create_client", create_client)
        response = client.get("/api/power")
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "hoymiles_station_balkonkraftwerk_current_power" in detail
        assert "unbekannt" in detail

    def test_uses_the_configured_device_list(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_power_devices(storage, [PowerDevice("sensor.waschmaschine_leistung", "Waschmaschine")])
        states = HAPPY_STATES | {"sensor.waschmaschine_leistung": "1200.0"}
        use_mock_ha(monkeypatch, MockHA(states))
        payload = client.get("/api/power").json()
        assert payload["devices"] == [
            {
                "entity_id": "sensor.waschmaschine_leistung",
                "name": "Waschmaschine",
                "value": 1200.0,
                "available": True,
            }
        ]


class TestPowerCache:
    def test_second_request_within_ttl_hits_the_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHA(HAPPY_STATES)
        use_mock_ha(monkeypatch, mock)
        first = client.get("/api/power").json()
        requests_after_first = mock.requests
        assert requests_after_first == 10  # 5 aggregates + 5 devices
        second = client.get("/api/power").json()
        assert mock.requests == requests_after_first
        assert second == first

    def test_cache_expires_after_ttl(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHA(HAPPY_STATES)
        use_mock_ha(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(power, "_now", lambda: clock["now"])
        client.get("/api/power")
        requests_after_first = mock.requests
        clock["now"] += power.CACHE_TTL_SECONDS + 0.1
        client.get("/api/power")
        assert mock.requests == 2 * requests_after_first

    def test_errors_are_cached_briefly_then_recover(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Errors get their own short-lived cache (see TestPowerErrorCache) so
        # a down HA instance is not hammered by every poller; recovery is
        # picked up once that error TTL — not the (longer) payload TTL —
        # has passed.
        mock = MockHA(HAPPY_STATES, down=True)
        use_mock_ha(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(power, "_now", lambda: clock["now"])
        assert client.get("/api/power").status_code == 502
        mock.down = False
        clock["now"] += power.ERROR_CACHE_TTL_SECONDS + 0.1
        assert client.get("/api/power").status_code == 200

    def test_saving_the_device_list_invalidates_the_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        states = HAPPY_STATES | {"sensor.neu_leistung": "5.0"}
        use_mock_ha(monkeypatch, MockHA(states))
        assert len(client.get("/api/power").json()["devices"]) == 5
        response = client.put(
            "/api/admin/settings/power",
            json={"devices": [{"entity_id": "sensor.neu_leistung", "name": "Neu"}]},
        )
        assert response.status_code == 200
        devices = client.get("/api/power").json()["devices"]
        assert [device["name"] for device in devices] == ["Neu"]


class TestPowerErrorCache:
    def test_error_is_served_from_cache_for_a_few_seconds(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHA(HAPPY_STATES, down=True)
        use_mock_ha(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(power, "_now", lambda: clock["now"])
        first = client.get("/api/power")
        assert first.status_code == 502
        requests_after_first = mock.requests
        assert requests_after_first == 10  # all entities attempted (gather, not short-circuit)
        clock["now"] += power.ERROR_CACHE_TTL_SECONDS - 0.1
        second = client.get("/api/power")
        assert second.status_code == 502
        assert second.json() == first.json()
        assert mock.requests == requests_after_first  # served from the error cache

    def test_error_cache_expires_after_its_ttl(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockHA(HAPPY_STATES, down=True)
        use_mock_ha(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(power, "_now", lambda: clock["now"])
        client.get("/api/power")
        requests_after_first = mock.requests
        clock["now"] += power.ERROR_CACHE_TTL_SECONDS + 0.1
        client.get("/api/power")
        assert mock.requests > requests_after_first


@pytest.mark.anyio
class TestFetchSnapshotLock:
    async def test_concurrent_misses_trigger_only_one_fetch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        power.reset_cache()
        mock = MockHA(HAPPY_STATES)
        use_mock_ha(monkeypatch, mock)
        results = await asyncio.gather(*(power._fetch_snapshot() for _ in range(5)))
        assert mock.requests == 10  # 5 aggregates + 5 devices, fetched exactly once
        for result in results:
            assert result == results[0]


class TestCreateClient:
    def test_defaults_to_supervisor_api_with_supervisor_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HA_API_URL", raising=False)
        monkeypatch.delenv("HA_API_TOKEN", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-token")
        ha_client = power.create_client()
        try:
            # httpx normalizes the base URL with a trailing slash.
            assert str(ha_client.base_url) == "http://supervisor/core/api/"
            assert ha_client.headers["Authorization"] == "Bearer super-token"
            assert ha_client.timeout.read == power.REQUEST_TIMEOUT_SECONDS
        finally:
            # Close synchronously — the client was never used.
            del ha_client

    def test_env_overrides_for_local_development(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_API_URL", "http://192.168.1.3:8123/api")
        monkeypatch.setenv("HA_API_TOKEN", "dev-token")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-token")
        ha_client = power.create_client()
        try:
            assert str(ha_client.base_url) == "http://192.168.1.3:8123/api/"
            assert ha_client.headers["Authorization"] == "Bearer dev-token"
        finally:
            del ha_client

    def test_default_url_explicitly_set_still_uses_supervisor_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Setting HA_API_URL to exactly the default is not an override.
        monkeypatch.setenv("HA_API_URL", power.DEFAULT_HA_API_URL)
        monkeypatch.delenv("HA_API_TOKEN", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-token")
        ha_client = power.create_client()
        try:
            assert ha_client.headers["Authorization"] == "Bearer super-token"
        finally:
            del ha_client

    def test_url_override_without_ha_api_token_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A custom HA_API_URL must not silently fall back to the
        # supervisor token (that token is only valid for the supervisor
        # proxy, and reusing it for another URL would be a mismatch that
        # is easy to miss) — fail fast with a clear error instead.
        monkeypatch.setenv("HA_API_URL", "http://192.168.1.3:8123/api")
        monkeypatch.delenv("HA_API_TOKEN", raising=False)
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-token")
        with pytest.raises(power.MissingHomeAssistantTokenError):
            power.create_client()

    def test_url_override_with_ha_api_token_ignores_supervisor_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HA_API_URL", "http://192.168.1.3:8123/api")
        monkeypatch.setenv("HA_API_TOKEN", "dev-token")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-token")
        ha_client = power.create_client()
        try:
            assert ha_client.headers["Authorization"] == "Bearer dev-token"
        finally:
            del ha_client
