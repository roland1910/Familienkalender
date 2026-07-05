"""Tests for the SQLite storage layer (sources and events)."""

import os
import sqlite3
import stat
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


class TestDbFilePermissions:
    def test_db_file_is_created(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        assert storage.db_path.exists()

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX file permissions do not apply on Windows"
    )
    def test_db_file_is_owner_only(self, tmp_path: Path) -> None:
        # The database holds source configs incl. app passwords — the
        # file must be private to the app user from the moment it exists.
        storage = make_storage(tmp_path)
        assert stat.S_IMODE(storage.db_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(
        os.name != "posix", reason="POSIX file permissions do not apply on Windows"
    )
    def test_existing_db_file_is_tightened(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        db_path.touch()
        db_path.chmod(0o644)
        Storage(db_path)
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


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


class TestShortcode:
    """The per-source shortcode used as a title prefix in the ICS feed."""

    def test_shortcode_defaults_to_empty(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_source(type="google", name="Marina", config={})
        assert storage.list_sources()[0].shortcode == ""

    def test_shortcode_roundtrip_via_add_and_update(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, shortcode="RMV"
        )
        assert storage.get_source(source_id).shortcode == "RMV"
        assert storage.update_source(source_id, shortcode="RX") is True
        assert storage.get_source(source_id).shortcode == "RX"

    def test_shortcode_can_be_cleared(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, shortcode="RMV"
        )
        storage.update_source(source_id, shortcode="")
        assert storage.get_source(source_id).shortcode == ""

    @pytest.mark.parametrize("bad", ["TOOLONG", "rx", "R X", "R<b>", "ÄÖ", "R-1"])
    def test_invalid_shortcodes_are_rejected(self, tmp_path: Path, bad: str) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        with pytest.raises(ValueError):
            storage.update_source(source_id, shortcode=bad)
        with pytest.raises(ValueError):
            storage.add_source(type="caldav", name="X", config={}, shortcode=bad)

    def test_update_without_shortcode_keeps_it(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, shortcode="RMV"
        )
        storage.update_source(source_id, name="Firma neu")
        assert storage.get_source(source_id).shortcode == "RMV"

    def test_existing_db_without_column_is_migrated(self, tmp_path: Path) -> None:
        """A database from an older version gains the column (default empty)."""
        db_path = tmp_path / "familienkalender.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                config TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                display_mode TEXT NOT NULL DEFAULT 'full',
                last_sync_at TEXT,
                last_sync_error TEXT
            );
            INSERT INTO sources (type, name) VALUES ('google', 'Marina');
            """
        )
        conn.commit()
        conn.close()
        storage = Storage(db_path)
        source = storage.list_sources()[0]
        assert source.name == "Marina"
        assert source.shortcode == ""
        assert storage.update_source(source.id, shortcode="RX") is True
        assert storage.get_source(source.id).shortcode == "RX"

    def test_instantiating_storage_twice_on_the_same_db_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """Re-running the schema/migration on an up-to-date database is a no-op.

        The add-on process restarts (watchdog, manual restart, upgrade)
        create a fresh Storage on the same db_path every time — this must
        never fail and must never touch existing data.
        """
        db_path = tmp_path / "familienkalender.db"
        first = Storage(db_path)
        source_id = first.add_source(
            type="caldav", name="Firma", config={}, shortcode="RX"
        )
        first.sync_events(
            source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW
        )

        second = Storage(db_path)  # must not raise

        sources = second.list_sources()
        assert len(sources) == 1
        assert sources[0].name == "Firma"
        assert sources[0].shortcode == "RX"
        assert len(second.get_events(WINDOW_START, WINDOW_END)) == 1


class TestSourceCrud:
    def test_get_source_returns_the_source(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="google", name="Marina", config={"a": 1})
        source = storage.get_source(source_id)
        assert source is not None
        assert source.name == "Marina"
        assert source.config == {"a": 1}

    def test_get_missing_source_returns_none(self, tmp_path: Path) -> None:
        assert make_storage(tmp_path).get_source(99) is None

    def test_update_source_changes_only_given_fields(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={"url": "https://a"}, display_mode="filtered"
        )
        assert storage.update_source(source_id, name="Firma neu", enabled=False) is True
        source = storage.get_source(source_id)
        assert source.name == "Firma neu"
        assert source.enabled is False
        assert source.display_mode == "filtered"
        assert source.config == {"url": "https://a"}

    def test_update_source_config_and_display_mode(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.update_source(source_id, config={"url": "https://b"}, display_mode="filtered")
        source = storage.get_source(source_id)
        assert source.config == {"url": "https://b"}
        assert source.display_mode == "filtered"

    def test_update_source_rejects_invalid_display_mode(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        with pytest.raises(ValueError):
            storage.update_source(source_id, display_mode="partial")

    def test_update_missing_source_returns_false(self, tmp_path: Path) -> None:
        assert make_storage(tmp_path).update_source(99, name="X") is False

    def test_delete_source_removes_source_and_events(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="google", name="Marina", config={})
        storage.sync_events(
            source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        assert storage.delete_source(source_id) is True
        assert storage.list_sources() == []
        assert storage.get_events(WINDOW_START, WINDOW_END) == []

    def test_delete_missing_source_returns_false(self, tmp_path: Path) -> None:
        assert make_storage(tmp_path).delete_source(99) is False

    def test_count_events_by_source(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        first = storage.add_source(type="google", name="Marina", config={})
        second = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            first,
            [timed_event(uid="a"), timed_event(uid="b")],
            WINDOW_START,
            WINDOW_END,
            synced_at=NOW,
        )
        assert storage.count_events_by_source() == {first: 2, second: 0}


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

    def test_event_moved_to_new_start_replaces_old_instance(self, tmp_path: Path) -> None:
        # Moving an event changes its start — part of the upsert key
        # (source_id, uid, start). The old row must be deleted, not kept
        # alongside the new one as a duplicate.
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        original = timed_event(
            uid="moved",
            start=datetime(2026, 7, 10, 8, 0, tzinfo=UTC),
            end=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
        )
        storage.sync_events(source_id, [original], WINDOW_START, WINDOW_END, synced_at=NOW)

        moved = timed_event(
            uid="moved",
            start=datetime(2026, 7, 11, 14, 0, tzinfo=UTC),
            end=datetime(2026, 7, 11, 15, 0, tzinfo=UTC),
        )
        storage.sync_events(source_id, [moved], WINDOW_START, WINDOW_END, synced_at=NOW)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert len(stored) == 1
        assert stored[0].event.uid == "moved"
        assert stored[0].event.start == datetime(2026, 7, 11, 14, 0, tzinfo=UTC)

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

    def test_disabled_source_events_are_hidden(self, tmp_path: Path) -> None:
        # A disabled source's events stay in the DB (no data loss) but must
        # not appear in get_events — the single choke point behind both
        # /api/events and the ICS feed.
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(uid="old-event")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.update_source(source_id, enabled=False)

        assert storage.get_events(WINDOW_START, WINDOW_END) == []

    def test_reenabled_source_events_reappear(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(uid="old-event")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.update_source(source_id, enabled=False)
        assert storage.get_events(WINDOW_START, WINDOW_END) == []

        storage.update_source(source_id, enabled=True)
        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert [item.event.uid for item in stored] == ["old-event"]

    def test_disabled_source_events_do_not_hide_other_sources(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        disabled_source = storage.add_source(type="caldav", name="Firma", config={})
        enabled_source = storage.add_source(type="google", name="Marina", config={})
        storage.sync_events(
            disabled_source, [timed_event(uid="a")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.sync_events(
            enabled_source, [timed_event(uid="b")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        storage.update_source(disabled_source, enabled=False)

        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert [item.event.uid for item in stored] == ["b"]


class TestSettings:
    def test_missing_setting_returns_none(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        assert storage.get_setting("evening_boundary") is None

    def test_set_and_get_roundtrip(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_setting("evening_boundary", "18:30")
        assert storage.get_setting("evening_boundary") == "18:30"

    def test_set_overwrites_existing_value(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_setting("evening_boundary", "18:30")
        storage.set_setting("evening_boundary", "17:00")
        assert storage.get_setting("evening_boundary") == "17:00"

    def test_settings_survive_reopening(self, tmp_path: Path) -> None:
        make_storage(tmp_path).set_setting("google_client_id", "abc.apps")
        assert make_storage(tmp_path).get_setting("google_client_id") == "abc.apps"

    def test_delete_setting(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_setting("evening_boundary", "18:30")
        storage.delete_setting("evening_boundary")
        assert storage.get_setting("evening_boundary") is None

    def test_delete_missing_setting_is_a_noop(self, tmp_path: Path) -> None:
        make_storage(tmp_path).delete_setting("nope")


class TestConnectionSettings:
    def test_connections_use_busy_timeout(self, tmp_path: Path) -> None:
        # Concurrent writers (periodic sync vs. manual sync/API) must wait
        # instead of failing immediately with "database is locked".
        storage = make_storage(tmp_path)
        with storage._connect() as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms == 5000


class TestDayTags:
    def test_no_tags_initially(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        assert storage.get_day_tags(date(2026, 7, 1), date(2026, 7, 31)) == {}

    def test_set_and_get_tags_for_a_day(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["😀", "⭐"])
        tags = storage.get_day_tags(date(2026, 7, 1), date(2026, 7, 31))
        assert tags == {"2026-07-10": ["😀", "⭐"]}

    def test_order_is_preserved(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["⭐", "😀", "🎉"])
        tags = storage.get_day_tags(date(2026, 7, 10), date(2026, 7, 10))
        assert tags["2026-07-10"] == ["⭐", "😀", "🎉"]

    def test_set_replaces_previous_tags(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["😀", "⭐"])
        storage.set_day_tags(date(2026, 7, 10), ["🎂"])
        tags = storage.get_day_tags(date(2026, 7, 10), date(2026, 7, 10))
        assert tags == {"2026-07-10": ["🎂"]}

    def test_empty_list_clears_the_day(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["😀"])
        storage.set_day_tags(date(2026, 7, 10), [])
        assert storage.get_day_tags(date(2026, 7, 10), date(2026, 7, 10)) == {}

    def test_range_is_inclusive_and_filters_outside_days(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 6, 30), ["😀"])
        storage.set_day_tags(date(2026, 7, 1), ["⭐"])
        storage.set_day_tags(date(2026, 7, 31), ["🎉"])
        storage.set_day_tags(date(2026, 8, 1), ["🎂"])
        tags = storage.get_day_tags(date(2026, 7, 1), date(2026, 7, 31))
        assert tags == {"2026-07-01": ["⭐"], "2026-07-31": ["🎉"]}

    def test_unknown_emoji_is_rejected(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        with pytest.raises(ValueError):
            storage.set_day_tags(date(2026, 7, 10), ["💩"])

    def test_free_text_is_rejected(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        with pytest.raises(ValueError):
            storage.set_day_tags(date(2026, 7, 10), ["<script>alert(1)</script>"])

    def test_rejected_set_leaves_existing_tags_untouched(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["😀"])
        with pytest.raises(ValueError):
            storage.set_day_tags(date(2026, 7, 10), ["💩"])
        assert storage.get_day_tags(date(2026, 7, 10), date(2026, 7, 10)) == {
            "2026-07-10": ["😀"]
        }

    def test_more_than_max_tags_is_rejected(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        with pytest.raises(ValueError):
            storage.set_day_tags(date(2026, 7, 10), ["😀", "⭐", "🎉", "🎂"])

    def test_duplicates_are_collapsed(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.set_day_tags(date(2026, 7, 10), ["😀", "😀", "⭐"])
        tags = storage.get_day_tags(date(2026, 7, 10), date(2026, 7, 10))
        assert tags["2026-07-10"] == ["😀", "⭐"]

    def test_tags_survive_reopening(self, tmp_path: Path) -> None:
        make_storage(tmp_path).set_day_tags(date(2026, 7, 10), ["🌞"])
        tags = make_storage(tmp_path).get_day_tags(date(2026, 7, 10), date(2026, 7, 10))
        assert tags == {"2026-07-10": ["🌞"]}


class TestTagOptions:
    def test_catalog_has_named_unique_entries(self) -> None:
        from app.models import TAG_OPTIONS

        ids = [option.id for option in TAG_OPTIONS]
        emojis = [option.emoji for option in TAG_OPTIONS]
        assert len(ids) == len(set(ids))
        assert len(emojis) == len(set(emojis))
        assert "😀" in emojis
        assert "🙁" in emojis
