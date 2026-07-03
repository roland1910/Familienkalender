"""Tests for the demo data seed script (local development and E2E tests)."""

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from app.models import LOCAL_TZ
from app.storage import Storage
from scripts.seed_demo import ensure_seed_target_allowed, seed_demo

TODAY = date(2026, 7, 3)


def _storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "familienkalender.db")


def _all_events(storage: Storage) -> list:
    range_start = datetime.combine(TODAY - timedelta(days=60), time.min, tzinfo=LOCAL_TZ)
    range_end = datetime.combine(TODAY + timedelta(days=90), time.min, tzinfo=LOCAL_TZ)
    return storage.get_events(range_start, range_end)


def test_seed_creates_three_sources(tmp_path: Path) -> None:
    seed_demo(tmp_path, today=TODAY)
    sources = _storage(tmp_path).list_sources()
    assert [(s.name, s.display_mode) for s in sources] == [
        ("Marina", "full"),
        ("Kunde", "filtered"),
        ("Firma", "filtered"),
    ]


def test_seed_is_idempotent(tmp_path: Path) -> None:
    seed_demo(tmp_path, today=TODAY)
    first = _all_events(_storage(tmp_path))
    seed_demo(tmp_path, today=TODAY)
    storage = _storage(tmp_path)
    assert len(storage.list_sources()) == 3
    assert len(_all_events(storage)) == len(first)


def test_seed_creates_events_around_today(tmp_path: Path) -> None:
    seed_demo(tmp_path, today=TODAY)
    events = _all_events(_storage(tmp_path))
    assert len(events) >= 10
    titles = {item.event.title for item in events}
    # Umlauts survive the round trip.
    assert any("Fußball" in title for title in titles)


def test_seed_includes_xss_payload_titles(tmp_path: Path) -> None:
    """The demo data must contain hostile titles for manual XSS verification."""
    seed_demo(tmp_path, today=TODAY)
    titles = {item.event.title for item in _all_events(_storage(tmp_path))}
    assert any("<script>" in title for title in titles)
    assert any("<img" in title and "onerror=" in title for title in titles)


def test_seed_includes_multi_day_event(tmp_path: Path) -> None:
    seed_demo(tmp_path, today=TODAY)
    events = _all_events(_storage(tmp_path))
    multi_day = [
        item
        for item in events
        if item.event.all_day and (item.event.end - item.event.start).days >= 3
    ]
    assert multi_day, "expected at least one all-day event spanning 3+ days"


def test_seed_includes_overflow_day_with_many_events(tmp_path: Path) -> None:
    """One day carries 6+ events so the month cell overflow popover shows up."""
    seed_demo(tmp_path, today=TODAY)
    events = _all_events(_storage(tmp_path))
    per_day: dict[date, int] = {}
    for item in events:
        start = item.event.start
        day = start.astimezone(LOCAL_TZ).date() if isinstance(start, datetime) else start
        per_day[day] = per_day.get(day, 0) + 1
    assert max(per_day.values()) >= 6


def test_seed_filtered_source_has_droppable_daytime_event(tmp_path: Path) -> None:
    """The demo exercises the family filter: a plain daytime meeting exists
    in a filtered source (stored, but hidden by the events API)."""
    seed_demo(tmp_path, today=TODAY)
    events = _all_events(_storage(tmp_path))
    filtered_daytime = [
        item
        for item in events
        if item.display_mode == "filtered"
        and not item.event.all_day
        and item.event.end.astimezone(LOCAL_TZ).time() <= time(17, 0)
        and item.event.start.astimezone(LOCAL_TZ).date()
        == item.event.end.astimezone(LOCAL_TZ).date()
    ]
    assert filtered_daytime, "expected a daytime meeting in a filtered source"


def test_seed_refuses_prod_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeding into the add-on's production /data must abort by default.

    The guard compares paths platform-independently (POSIX form), so the
    test works on Windows too, where /data does not exist.
    """
    monkeypatch.delenv("FAMILIENKALENDER_ALLOW_PROD_SEED", raising=False)
    with pytest.raises(SystemExit, match="FAMILIENKALENDER_ALLOW_PROD_SEED"):
        ensure_seed_target_allowed(Path("/data"))


def test_seed_demo_itself_refuses_prod_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard sits in seed_demo, not only in main — every caller is safe."""
    monkeypatch.delenv("FAMILIENKALENDER_ALLOW_PROD_SEED", raising=False)
    with pytest.raises(SystemExit):
        seed_demo(Path("/data"), today=TODAY)


def test_seed_prod_data_dir_allowed_with_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAMILIENKALENDER_ALLOW_PROD_SEED", "1")
    ensure_seed_target_allowed(Path("/data"))  # must not raise


def test_seed_other_dirs_are_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FAMILIENKALENDER_ALLOW_PROD_SEED", raising=False)
    ensure_seed_target_allowed(tmp_path)  # must not raise
