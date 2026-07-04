"""Tests for typed access to persisted settings (app.settings)."""

import json
from datetime import time
from pathlib import Path

import pytest

from app.settings import (
    DEFAULT_POWER_DEVICES,
    EVENING_BOUNDARY_KEY,
    POWER_DEVICES_KEY,
    PowerDevice,
    get_evening_boundary,
    get_power_devices,
    set_power_devices,
)
from app.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


class TestEveningBoundary:
    def test_defaults_to_17_00(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVENING_BOUNDARY", raising=False)
        assert get_evening_boundary(storage) == time(17, 0)

    def test_stored_setting_wins(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENING_BOUNDARY", "19:00")
        storage.set_setting(EVENING_BOUNDARY_KEY, "18:30")
        assert get_evening_boundary(storage) == time(18, 30)

    def test_env_var_is_fallback_without_setting(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENING_BOUNDARY", "19:15")
        assert get_evening_boundary(storage) == time(19, 15)

    def test_invalid_stored_value_falls_back_to_default(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("EVENING_BOUNDARY", raising=False)
        storage.set_setting(EVENING_BOUNDARY_KEY, "quatsch")
        assert get_evening_boundary(storage) == time(17, 0)

    def test_invalid_env_value_falls_back_to_default(
        self, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVENING_BOUNDARY", "25:99")
        assert get_evening_boundary(storage) == time(17, 0)


class TestPowerDevices:
    def test_defaults_without_stored_setting(self, storage: Storage) -> None:
        devices = get_power_devices(storage)
        assert devices == list(DEFAULT_POWER_DEVICES)
        # The defaults are the household's smart plugs with German names.
        assert PowerDevice("sensor.kuhlschrank_leistung", "Kühlschrank") in devices

    def test_roundtrip_persists_devices(self, storage: Storage) -> None:
        devices = [
            PowerDevice("sensor.kuhlschrank_leistung", "Kühlschrank"),
            PowerDevice("sensor.neue_steckdose_leistung", "Neue Steckdose"),
        ]
        set_power_devices(storage, devices)
        assert get_power_devices(storage) == devices

    def test_empty_list_is_a_valid_stored_value(self, storage: Storage) -> None:
        set_power_devices(storage, [])
        assert get_power_devices(storage) == []

    def test_invalid_json_falls_back_to_defaults(self, storage: Storage) -> None:
        storage.set_setting(POWER_DEVICES_KEY, "kaputt")
        assert get_power_devices(storage) == list(DEFAULT_POWER_DEVICES)

    def test_wrong_shape_falls_back_to_defaults(self, storage: Storage) -> None:
        storage.set_setting(POWER_DEVICES_KEY, json.dumps([{"foo": "bar"}]))
        assert get_power_devices(storage) == list(DEFAULT_POWER_DEVICES)
