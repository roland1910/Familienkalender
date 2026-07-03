"""Tests for typed access to persisted settings (app.settings)."""

from datetime import time
from pathlib import Path

import pytest

from app.settings import EVENING_BOUNDARY_KEY, get_evening_boundary
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
