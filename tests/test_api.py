"""Tests for the sync/events API endpoints."""

import time as time_module
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import CalendarEvent
from app.storage import Storage, default_db_path

BERLIN = ZoneInfo("Europe/Berlin")

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


def seed_two_sources(storage: Storage) -> tuple[int, int]:
    """A full source (Marina) and a filtered source (Firma) with events."""
    full_id = storage.add_source(
        type="google", name="Marina", config={"calendar_id": "m@example.com"},
        display_mode="full",
    )
    filtered_id = storage.add_source(
        type="caldav",
        name="Firma",
        config={"calendar_url": "https://nc/cal/", "app_password": "geheim"},
        display_mode="filtered",
    )
    storage.sync_events(
        full_id,
        [
            CalendarEvent(
                uid="marina-morning",
                title="Yoga",
                start=datetime(2026, 7, 10, 9, 0, tzinfo=BERLIN),
                end=datetime(2026, 7, 10, 10, 0, tzinfo=BERLIN),
                all_day=False,
            )
        ],
        WINDOW_START,
        WINDOW_END,
        synced_at=NOW,
    )
    storage.sync_events(
        filtered_id,
        [
            CalendarEvent(
                uid="firma-morning",
                title="Standup",
                start=datetime(2026, 7, 10, 10, 0, tzinfo=BERLIN),
                end=datetime(2026, 7, 10, 10, 30, tzinfo=BERLIN),
                all_day=False,
            ),
            CalendarEvent(
                uid="firma-evening",
                title="Kundentermin",
                start=datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN),
                end=datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN),
                all_day=False,
                location="München",
            ),
        ],
        WINDOW_START,
        WINDOW_END,
        synced_at=NOW,
    )
    return full_id, filtered_id


class TestConfigEndpoint:
    """GET /api/config — public frontend configuration (no admin gate)."""

    def test_default_view_defaults_to_month(self, client: TestClient) -> None:
        response = client.get("/api/config")
        assert response.status_code == 200
        assert response.json()["default_view"] == "month"

    def test_reflects_the_stored_setting(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.set_setting("default_view", "week")
        assert client.get("/api/config").json()["default_view"] == "week"

    def test_invalid_stored_value_falls_back_to_month(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.set_setting("default_view", "quatsch")
        assert client.get("/api/config").json()["default_view"] == "month"

    def test_screensaver_default_defaults_to_off(self, client: TestClient) -> None:
        assert client.get("/api/config").json()["screensaver_default"] == "off"

    def test_screensaver_default_reflects_the_stored_setting(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.set_setting("screensaver_default", "on")
        assert client.get("/api/config").json()["screensaver_default"] == "on"

    def test_invalid_screensaver_default_falls_back_to_off(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.set_setting("screensaver_default", "quatsch")
        assert client.get("/api/config").json()["screensaver_default"] == "off"

    def test_contains_only_the_known_fields(self, client: TestClient) -> None:
        # The endpoint is public — it must never grow secrets by accident.
        assert set(client.get("/api/config").json().keys()) == {
            "default_view",
            "screensaver_default",
        }


class TestSourcesEndpoint:
    def test_lists_sources_without_secrets(self, client: TestClient, storage: Storage) -> None:
        seed_two_sources(storage)
        response = client.get("/api/sources")
        assert response.status_code == 200
        sources = response.json()["sources"]
        assert [source["name"] for source in sources] == ["Marina", "Firma"]
        body = response.text
        assert "geheim" not in body
        assert "config" not in body
        assert sources[1]["display_mode"] == "filtered"
        assert sources[1]["enabled"] is True
        assert "last_sync_at" in sources[1]
        assert "last_sync_error" in sources[1]

    def test_lists_sources_with_their_color(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.add_source(type="google", name="Marina", config={}, color="#ff0066")
        storage.add_source(type="google", name="Valentin", config={})
        sources = client.get("/api/sources").json()["sources"]
        assert sources[0]["color"] == "#ff0066"
        assert sources[1]["color"] == ""  # empty = frontend palette default


class TestEventsEndpoint:
    def test_returns_events_in_range(self, client: TestClient, storage: Storage) -> None:
        seed_two_sources(storage)
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        assert response.status_code == 200
        events = response.json()["events"]
        assert [event["uid"] for event in events] == ["marina-morning", "firma-evening"]

    def test_filtered_source_hides_intra_day_events(
        self, client: TestClient, storage: Storage
    ) -> None:
        seed_two_sources(storage)
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        uids = [event["uid"] for event in response.json()["events"]]
        assert "firma-morning" not in uids  # filtered: plain daytime meeting
        assert "marina-morning" in uids  # full source keeps everything

    def test_serializes_events_in_local_time(
        self, client: TestClient, storage: Storage
    ) -> None:
        seed_two_sources(storage)
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        event = next(
            item for item in response.json()["events"] if item["uid"] == "firma-evening"
        )
        assert event["title"] == "Kundentermin"
        assert event["location"] == "München"
        assert event["source_name"] == "Firma"
        assert event["all_day"] is False
        assert event["start"] == "2026-07-10T16:00:00+02:00"
        assert event["end"] == "2026-07-10T18:00:00+02:00"

    def test_events_carry_the_source_color(
        self, client: TestClient, storage: Storage
    ) -> None:
        full_id, _ = seed_two_sources(storage)
        storage.update_source(full_id, color="#ff0066")
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        events = {event["uid"]: event for event in response.json()["events"]}
        assert events["marina-morning"]["source_color"] == "#ff0066"
        assert events["firma-evening"]["source_color"] == ""  # palette default

    def test_serializes_all_day_events_as_dates(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="google", name="Marina", config={})
        storage.sync_events(
            source_id,
            [
                CalendarEvent(
                    uid="ferien",
                    title="Ferien",
                    start=date(2026, 7, 20),
                    end=date(2026, 7, 25),
                    all_day=True,
                )
            ],
            WINDOW_START,
            WINDOW_END,
            synced_at=NOW,
        )
        response = client.get("/api/events?from=2026-07-19&to=2026-07-26")
        event = response.json()["events"][0]
        assert event["all_day"] is True
        assert event["start"] == "2026-07-20"
        assert event["end"] == "2026-07-25"

    def test_range_is_inclusive_of_both_days(
        self, client: TestClient, storage: Storage
    ) -> None:
        seed_two_sources(storage)
        # Events are on 2026-07-10; a range ending the day before is empty.
        response = client.get("/api/events?from=2026-07-01&to=2026-07-09")
        assert response.json()["events"] == []
        response = client.get("/api/events?from=2026-07-09&to=2026-07-10")
        assert len(response.json()["events"]) == 2

    def test_missing_params_are_rejected(self, client: TestClient, storage: Storage) -> None:
        assert client.get("/api/events").status_code == 422

    def test_from_after_to_is_rejected(self, client: TestClient, storage: Storage) -> None:
        response = client.get("/api/events?from=2026-07-10&to=2026-07-01")
        assert response.status_code == 400

    def test_evening_boundary_is_configurable_via_env(
        self,
        client: TestClient,
        storage: Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_two_sources(storage)
        # Boundary 18:30: the 16:00-18:00 Kundentermin no longer qualifies.
        monkeypatch.setenv("EVENING_BOUNDARY", "18:30")
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        uids = [event["uid"] for event in response.json()["events"]]
        assert "firma-evening" not in uids
        assert "marina-morning" in uids

    def test_evening_boundary_setting_wins_over_env(
        self,
        client: TestClient,
        storage: Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seed_two_sources(storage)
        # Env would keep the 16:00-18:00 Kundentermin; the persisted admin
        # setting (18:30) takes precedence and hides it.
        monkeypatch.setenv("EVENING_BOUNDARY", "15:00")
        storage.set_setting("evening_boundary", "18:30")
        response = client.get("/api/events?from=2026-07-10&to=2026-07-10")
        uids = [event["uid"] for event in response.json()["events"]]
        assert "firma-evening" not in uids
        assert "marina-morning" in uids


class TestSyncEndpoint:
    def test_returns_409_while_a_sync_is_running(
        self,
        client: TestClient,
        storage: Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class HeldLock:
            def locked(self) -> bool:
                return True

        monkeypatch.setattr("app.sync.SYNC_LOCK", HeldLock())
        response = client.post("/api/sync")
        assert response.status_code == 409
        assert "läuft bereits" in response.json()["detail"]

    def test_manual_sync_returns_per_source_results(
        self,
        client: TestClient,
        storage: Storage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config={})

        async def fake_sync_all(storage_arg, **kwargs):
            return {source_id: None}

        monkeypatch.setattr("app.main.sync_all", fake_sync_all)
        response = client.post("/api/sync")
        assert response.status_code == 200
        assert response.json() == {"results": {str(source_id): None}}


class TestPeriodicSyncLifespan:
    def test_periodic_sync_starts_with_lifespan(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "3600")
        calls: list[object] = []

        async def fake_sync_all(storage_arg, **kwargs):
            calls.append(storage_arg)
            return {}

        monkeypatch.setattr("app.sync.sync_all", fake_sync_all)
        with TestClient(app, client=("127.0.0.1", 50000)) as running_client:
            running_client.get("/api/health")
            deadline = time_module.time() + 2
            while not calls and time_module.time() < deadline:
                time_module.sleep(0.02)
        assert calls, "periodic sync did not run within the lifespan"

    def test_periodic_sync_can_be_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
        calls: list[object] = []

        async def fake_sync_all(storage_arg, **kwargs):
            calls.append(storage_arg)
            return {}

        monkeypatch.setattr("app.sync.sync_all", fake_sync_all)
        with TestClient(app, client=("127.0.0.1", 50000)) as running_client:
            running_client.get("/api/health")
            time_module.sleep(0.1)
        assert calls == []
