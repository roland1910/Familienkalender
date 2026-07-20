"""Tests for typed access to persisted settings (app.settings)."""

import json
from datetime import time
from pathlib import Path

import pytest

from app.settings import (
    DEFAULT_POWER_DEVICES,
    DEFAULT_VIEW_KEY,
    EVENING_BOUNDARY_KEY,
    FEED_PUBLIC_HOST_KEY,
    POWER_DEVICES_KEY,
    SCREENSAVER_DEFAULT_KEY,
    PowerDevice,
    get_default_view,
    get_evening_boundary,
    get_feed_public_host,
    get_power_devices,
    get_screensaver_default,
    is_valid_default_view,
    is_valid_public_host,
    is_valid_screensaver_default,
    set_default_view,
    set_feed_public_host,
    set_power_devices,
    set_screensaver_default,
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


class TestDefaultView:
    def test_defaults_to_month(self, storage: Storage) -> None:
        assert get_default_view(storage) == "month"

    def test_set_and_get_round_trip(self, storage: Storage) -> None:
        set_default_view(storage, "week")
        assert get_default_view(storage) == "week"
        set_default_view(storage, "month")
        assert get_default_view(storage) == "month"

    def test_invalid_stored_value_falls_back_to_month(self, storage: Storage) -> None:
        # Defense in depth: the admin API validates on write, but a value
        # written by another path must not leak to the frontend.
        storage.set_setting(DEFAULT_VIEW_KEY, "disco")
        assert get_default_view(storage) == "month"

    @pytest.mark.parametrize("view", ["month", "week"])
    def test_valid_views(self, view: str) -> None:
        assert is_valid_default_view(view)

    @pytest.mark.parametrize("view", ["", "day", "MONTH", "Woche", "week "])
    def test_invalid_views(self, view: str) -> None:
        assert not is_valid_default_view(view)


class TestScreensaverDefault:
    def test_defaults_to_off(self, storage: Storage) -> None:
        assert get_screensaver_default(storage) == "off"

    def test_set_and_get_round_trip(self, storage: Storage) -> None:
        set_screensaver_default(storage, "on")
        assert get_screensaver_default(storage) == "on"
        set_screensaver_default(storage, "off")
        assert get_screensaver_default(storage) == "off"

    def test_invalid_stored_value_falls_back_to_off(self, storage: Storage) -> None:
        # Defense in depth: the admin API validates on write, but a value
        # written by another path must not arm the screensaver by accident.
        storage.set_setting(SCREENSAVER_DEFAULT_KEY, "disco")
        assert get_screensaver_default(storage) == "off"

    @pytest.mark.parametrize("value", ["on", "off"])
    def test_valid_values(self, value: str) -> None:
        assert is_valid_screensaver_default(value)

    @pytest.mark.parametrize("value", ["", "ON", "true", "1", "an", "off "])
    def test_invalid_values(self, value: str) -> None:
        assert not is_valid_screensaver_default(value)


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

    def test_device_without_name_defaults_to_empty(self, storage: Storage) -> None:
        # A stored entry may carry no (or an empty) name — the display name
        # then comes from the HA friendly_name at render time.
        storage.set_setting(
            POWER_DEVICES_KEY,
            json.dumps(
                [
                    {"entity_id": "sensor.a_leistung"},
                    {"entity_id": "sensor.b_leistung", "name": ""},
                ]
            ),
        )
        assert get_power_devices(storage) == [
            PowerDevice("sensor.a_leistung", ""),
            PowerDevice("sensor.b_leistung", ""),
        ]

    def test_invalid_json_falls_back_to_defaults(self, storage: Storage) -> None:
        storage.set_setting(POWER_DEVICES_KEY, "kaputt")
        assert get_power_devices(storage) == list(DEFAULT_POWER_DEVICES)

    def test_wrong_shape_falls_back_to_defaults(self, storage: Storage) -> None:
        storage.set_setting(POWER_DEVICES_KEY, json.dumps([{"foo": "bar"}]))
        assert get_power_devices(storage) == list(DEFAULT_POWER_DEVICES)

    def test_entries_with_invalid_entity_id_are_skipped_defensively(
        self, storage: Storage
    ) -> None:
        # Defense in depth: even though the admin API validates on write,
        # get_power_devices re-checks on read and drops anything that
        # slipped through (e.g. a manually edited DB) instead of sending
        # it to HA or failing the whole list.
        storage.set_setting(
            POWER_DEVICES_KEY,
            json.dumps(
                [
                    {"entity_id": "sensor.ok_leistung", "name": "OK"},
                    {"entity_id": "<script>alert(1)</script>", "name": "Böse"},
                ]
            ),
        )
        devices = get_power_devices(storage)
        assert devices == [PowerDevice("sensor.ok_leistung", "OK")]


class TestFeedPublicHost:
    def test_missing_setting_is_none(self, storage: Storage) -> None:
        assert get_feed_public_host(storage) is None

    def test_set_and_get_round_trip(self, storage: Storage) -> None:
        set_feed_public_host(storage, "rnd.ignorelist.com")
        assert get_feed_public_host(storage) == "rnd.ignorelist.com"

    def test_empty_value_clears_the_host(self, storage: Storage) -> None:
        set_feed_public_host(storage, "rnd.ignorelist.com")
        set_feed_public_host(storage, "")
        assert get_feed_public_host(storage) is None

    def test_stored_garbage_is_ignored_on_read(self, storage: Storage) -> None:
        # Defense in depth: the admin API validates on write, but a value
        # written by another path must not leak into a generated URL.
        storage.set_setting(FEED_PUBLIC_HOST_KEY, "https://böse.example/pfad")
        assert get_feed_public_host(storage) is None

    @pytest.mark.parametrize(
        "host",
        ["rnd.ignorelist.com", "homeassistant.local", "192.168.1.3", "kalender"],
    )
    def test_valid_hosts(self, host: str) -> None:
        assert is_valid_public_host(host)

    @pytest.mark.parametrize(
        "host",
        [
            "",
            "https://rnd.ignorelist.com",
            "host:8098",
            "host/pfad",
            "mit leerzeichen",
            "-strich.de",
            "strich-.de",
            "punkt..doppelt",
            "ümlaut.example",
            "a" * 300,
        ],
    )
    def test_invalid_hosts(self, host: str) -> None:
        assert not is_valid_public_host(host)
