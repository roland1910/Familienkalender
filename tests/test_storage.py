"""Tests for the SQLite storage layer (sources and events)."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.models import CalendarEvent
from app.storage import Storage, resolve_data_dir

BERLIN_OFFSET_SUMMER = "+02:00"


def make_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def timed_event(
    uid: str = "uid-1",
    title: str = "Termin",
    start: datetime | None = None,
    end: datetime | None = None,
    location: str | None = None,
) -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title=title,
        start=start or datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
        end=end or datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        all_day=False,
        location=location,
    )


def all_day_event(uid: str = "uid-allday", days: int = 1) -> CalendarEvent:
    start = date(2026, 7, 12)
    return CalendarEvent(
        uid=uid,
        title="Ganztägig",
        start=start,
        end=date(2026, 7, 12 + days),
        all_day=True,
    )


WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


class TestDataDir:
    def test_env_variable_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path / "custom"))
        assert resolve_data_dir() == tmp_path / "custom"

    def test_defaults_to_local_data_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATA_DIR", raising=False)
        # Outside the container /data does not exist, so ./data is used.
        assert resolve_data_dir() in (Path("/data"), Path("data"))


class TestSources:
    def test_add_and_list_sources_roundtrip(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav",
            name="Firma",
            config={"calendar_url": "https://nc.example/cal", "username": "u"},
            display_mode="filtered",
        )
        sources = storage.list_sources()
        assert len(sources) == 1
        source = sources[0]
        assert source.id == source_id
        assert source.type == "caldav"
        assert source.name == "Firma"
        assert source.config == {"calendar_url": "https://nc.example/cal", "username": "u"}
        assert source.enabled is True
        assert source.display_mode == "filtered"
        assert source.last_sync_at is None
        assert source.last_sync_error is None

    def test_display_mode_defaults_to_full(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_source(type="google", name="Marina", config={})
        assert storage.list_sources()[0].display_mode == "full"

    def test_invalid_type_is_rejected(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        with pytest.raises(ValueError):
            storage.add_source(type="outlook", name="X", config={})

    def test_invalid_display_mode_is_rejected(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        with pytest.raises(ValueError):
            storage.add_source(type="google", name="X", config={}, display_mode="partial")

    def test_update_sync_status_success(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="google", name="Marina", config={})
        storage.update_sync_status(source_id, synced_at=NOW, error=None)
        source = storage.list_sources()[0]
        assert source.last_sync_at == NOW
        assert source.last_sync_error is None

    def test_update_sync_status_error(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="google", name="Marina", config={})
        storage.update_sync_status(source_id, synced_at=NOW, error="HTTP 500")
        source = storage.list_sources()[0]
        assert source.last_sync_error == "HTTP 500"


class TestEvents:
    def test_sync_events_inserts_and_reads_back(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        event = timed_event(location="Büro")
        storage.sync_events(source_id, [event], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert len(stored) == 1
        assert stored[0].source_id == source_id
        assert stored[0].event == event

    def test_all_day_event_roundtrip_keeps_dates(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        event = all_day_event(days=3)
        storage.sync_events(source_id, [event], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert stored[0].event.all_day is True
        assert stored[0].event.start == date(2026, 7, 12)
        assert stored[0].event.end == date(2026, 7, 15)

    def test_upsert_updates_changed_title_without_duplicating(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(title="Alt")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.sync_events(
            source_id, [timed_event(title="Neu")], WINDOW_START, WINDOW_END, synced_at=NOW
        )

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert len(stored) == 1
        assert stored[0].event.title == "Neu"

    def test_vanished_events_are_deleted(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        keep = timed_event(uid="keep")
        gone = timed_event(uid="gone", start=datetime(2026, 7, 11, 8, 0, tzinfo=UTC))
        storage.sync_events(source_id, [keep, gone], WINDOW_START, WINDOW_END, synced_at=NOW)
        storage.sync_events(source_id, [keep], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert [item.event.uid for item in stored] == ["keep"]

    def test_events_of_other_sources_are_untouched(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_a = storage.add_source(type="caldav", name="Firma", config={})
        source_b = storage.add_source(type="google", name="Marina", config={})
        storage.sync_events(
            source_a, [timed_event(uid="a")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.sync_events(
            source_b, [timed_event(uid="b")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        # Re-sync source A with nothing: only A's events vanish.
        storage.sync_events(source_a, [], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert [item.event.uid for item in stored] == ["b"]

    def test_get_events_filters_by_overlap(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        july = timed_event(uid="july", start=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
                           end=datetime(2026, 7, 10, 9, 0, tzinfo=UTC))
        september = timed_event(uid="sept", start=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
                                end=datetime(2026, 9, 10, 9, 0, tzinfo=UTC))
        storage.sync_events(
            source_id, [july, september], WINDOW_START, WINDOW_END, synced_at=NOW
        )

        stored = storage.get_events(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
        )
        assert [item.event.uid for item in stored] == ["july"]

    def test_get_events_includes_overlapping_all_day_event(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [all_day_event(days=3)], WINDOW_START, WINDOW_END, synced_at=NOW
        )

        # Query range starts in the middle of the all-day span.
        stored = storage.get_events(
            datetime(2026, 7, 13, tzinfo=UTC), datetime(2026, 7, 20, tzinfo=UTC)
        )
        assert len(stored) == 1

    def test_get_events_carries_source_metadata(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, display_mode="filtered"
        )
        storage.sync_events(source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert stored[0].source_name == "Firma"
        assert stored[0].display_mode == "filtered"

    def test_events_outside_sync_window_survive_a_sync(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        outside = timed_event(
            uid="outside",
            start=datetime(2026, 12, 24, 10, 0, tzinfo=UTC),
            end=datetime(2026, 12, 24, 11, 0, tzinfo=UTC),
        )
        wide_end = datetime(2027, 1, 1, tzinfo=UTC)
        storage.sync_events(source_id, [outside], WINDOW_START, wide_end, synced_at=NOW)
        # A later sync with a narrower window must not delete the outside event.
        storage.sync_events(source_id, [], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, wide_end)
        assert [item.event.uid for item in stored] == ["outside"]

    def test_events_are_sorted_by_start(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        late = timed_event(uid="late", start=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
                           end=datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
        early = timed_event(uid="early", start=datetime(2026, 7, 5, 8, 0, tzinfo=UTC),
                            end=datetime(2026, 7, 5, 9, 0, tzinfo=UTC))
        storage.sync_events(source_id, [late, early], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert [item.event.uid for item in stored] == ["early", "late"]


class TestConnectionSettings:
    def test_connections_use_busy_timeout(self, tmp_path: Path) -> None:
        # Concurrent writers (periodic sync vs. manual sync/API) must wait
        # instead of failing immediately with "database is locked".
        storage = make_storage(tmp_path)
        with storage._connect() as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms == 5000
