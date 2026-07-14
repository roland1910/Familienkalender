"""Tests for the SQLite storage layer (sources and events)."""

import os
import sqlite3
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.models import AuditEntry, BusyBlock, CalendarEvent
from app.storage import AUDIT_RETENTION_DAYS, Storage, resolve_data_dir

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

    def test_repeated_migration_of_a_legacy_db_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """A pre-shortcode/color/include_in_feed database survives two starts.

        Covers the column-by-column ALTER TABLE migrations in
        Storage._migrate for shortcode, color and include_in_feed together
        (each already has its own single-instantiation migration test above;
        this one exercises what actually happens on the Pi: the add-on
        restarts repeatedly against the same on-disk database). The
        include_in_feed backfill in particular must not silently re-run and
        clobber a value an admin already toggled by hand.
        """
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
            INSERT INTO sources (type, name, display_mode)
            VALUES ('google', 'Marina', 'full'),
                   ('caldav', 'Firma', 'filtered');
            """
        )
        conn.commit()
        conn.close()

        first = Storage(db_path)  # first migration: adds all three columns
        by_name = {source.name: source for source in first.list_sources()}
        assert by_name["Marina"].shortcode == ""
        assert by_name["Marina"].color == ""
        assert by_name["Marina"].include_in_feed is False
        assert by_name["Firma"].include_in_feed is True

        # An admin now hand-tunes both a migrated default and the new
        # columns before the add-on restarts again.
        firma_id = by_name["Firma"].id
        first.update_source(firma_id, include_in_feed=False, color="#123abc", shortcode="RX")

        second = Storage(db_path)  # second migration on the same db: no-op
        by_name_again = {source.name: source for source in second.list_sources()}
        assert by_name_again["Marina"].include_in_feed is False
        assert by_name_again["Firma"].include_in_feed is False  # not reset to True
        assert by_name_again["Firma"].color == "#123abc"
        assert by_name_again["Firma"].shortcode == "RX"


class TestSourceColor:
    """The optional admin-configured display color of a source."""

    def test_color_defaults_to_empty(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_source(type="google", name="Marina", config={})
        assert storage.list_sources()[0].color == ""

    def test_color_roundtrip_via_add_and_update(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, color="#ff0066"
        )
        assert storage.get_source(source_id).color == "#ff0066"
        assert storage.update_source(source_id, color="#00aa11") is True
        assert storage.get_source(source_id).color == "#00aa11"

    def test_color_can_be_cleared(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, color="#ff0066"
        )
        storage.update_source(source_id, color="")
        assert storage.get_source(source_id).color == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "red",  # named colors are not allowed
            "#FF0066",  # uppercase is rejected at the storage layer
            "#fff",  # short form
            "#ff00667f",  # alpha channel
            "#ff006",  # too short
            "ff0066",  # missing hash
            "#ff0066; background:url(x)",  # CSS injection attempt
            "var(--evil)",
        ],
    )
    def test_invalid_colors_are_rejected(self, tmp_path: Path, bad: str) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        with pytest.raises(ValueError):
            storage.update_source(source_id, color=bad)
        with pytest.raises(ValueError):
            storage.add_source(type="caldav", name="X", config={}, color=bad)

    def test_update_without_color_keeps_it(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, color="#ff0066"
        )
        storage.update_source(source_id, name="Firma neu")
        assert storage.get_source(source_id).color == "#ff0066"

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
                last_sync_error TEXT,
                shortcode TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO sources (type, name) VALUES ('google', 'Marina');
            """
        )
        conn.commit()
        conn.close()
        storage = Storage(db_path)
        source = storage.list_sources()[0]
        assert source.color == ""
        assert storage.update_source(source.id, color="#123abc") is True
        assert storage.get_source(source.id).color == "#123abc"

    def test_get_events_carries_the_source_color(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, color="#ff0066"
        )
        storage.sync_events(source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW)
        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert stored[0].color == "#ff0066"


class TestIncludeInFeed:
    """Per-source switch deciding whether the source feeds the ICS feed."""

    def test_default_follows_display_mode(self, tmp_path: Path) -> None:
        # Historical behaviour as default: filtered sources (Roland's work
        # calendars) feed the subscription, full sources (Marina, Valentin)
        # do not — she subscribes to the feed herself.
        storage = make_storage(tmp_path)
        filtered_id = storage.add_source(
            type="caldav", name="Firma", config={}, display_mode="filtered"
        )
        full_id = storage.add_source(
            type="google", name="Valentin", config={}, display_mode="full"
        )
        assert storage.get_source(filtered_id).include_in_feed is True
        assert storage.get_source(full_id).include_in_feed is False

    def test_explicit_value_wins_over_the_display_mode_default(
        self, tmp_path: Path
    ) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav",
            name="Firma",
            config={},
            display_mode="filtered",
            include_in_feed=False,
        )
        assert storage.get_source(source_id).include_in_feed is False

    def test_toggle_via_update(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, display_mode="filtered"
        )
        assert storage.update_source(source_id, include_in_feed=False) is True
        assert storage.get_source(source_id).include_in_feed is False
        storage.update_source(source_id, include_in_feed=True)
        assert storage.get_source(source_id).include_in_feed is True

    def test_update_without_value_keeps_it(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, display_mode="filtered"
        )
        storage.update_source(source_id, name="Firma neu")
        assert storage.get_source(source_id).include_in_feed is True

    def test_changing_display_mode_does_not_touch_the_flag(
        self, tmp_path: Path
    ) -> None:
        # Once the column exists the flag is independent — switching the
        # display mode must not silently change what the feed contains.
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="google", name="Valentin", config={}, display_mode="full"
        )
        storage.update_source(source_id, display_mode="filtered")
        assert storage.get_source(source_id).include_in_feed is False

    def test_existing_db_is_migrated_from_display_mode(self, tmp_path: Path) -> None:
        """Legacy rows get include_in_feed = (display_mode == 'filtered')."""
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
                last_sync_error TEXT,
                shortcode TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO sources (type, name, display_mode)
            VALUES ('google', 'Marina', 'full'),
                   ('caldav', 'Firma', 'filtered');
            """
        )
        conn.commit()
        conn.close()
        storage = Storage(db_path)
        by_name = {source.name: source for source in storage.list_sources()}
        assert by_name["Marina"].include_in_feed is False
        assert by_name["Firma"].include_in_feed is True

    def test_get_events_carries_the_flag(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, display_mode="filtered"
        )
        storage.sync_events(source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW)
        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert stored[0].include_in_feed is True


class TestFeedPriority:
    """Per-source precedence for collapsing duplicate events in the feed."""

    def test_defaults_to_zero(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_source(type="google", name="Marina", config={})
        assert storage.list_sources()[0].feed_priority == 0

    def test_roundtrip_via_add_and_update(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, feed_priority=10
        )
        assert storage.get_source(source_id).feed_priority == 10
        assert storage.update_source(source_id, feed_priority=-5) is True
        assert storage.get_source(source_id).feed_priority == -5

    @pytest.mark.parametrize("bad", [101, -101, 1000])
    def test_out_of_range_values_are_rejected(self, tmp_path: Path, bad: int) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        with pytest.raises(ValueError):
            storage.update_source(source_id, feed_priority=bad)
        with pytest.raises(ValueError):
            storage.add_source(type="caldav", name="X", config={}, feed_priority=bad)

    def test_update_without_value_keeps_it(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, feed_priority=7
        )
        storage.update_source(source_id, name="Firma neu")
        assert storage.get_source(source_id).feed_priority == 7

    def test_existing_db_without_column_is_migrated_to_zero(
        self, tmp_path: Path
    ) -> None:
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
                last_sync_error TEXT,
                shortcode TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO sources (type, name) VALUES ('google', 'Marina');
            """
        )
        conn.commit()
        conn.close()
        storage = Storage(db_path)
        source = storage.list_sources()[0]
        assert source.feed_priority == 0
        assert storage.update_source(source.id, feed_priority=3) is True
        assert storage.get_source(source.id).feed_priority == 3

    def test_get_events_carries_the_priority(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(
            type="caldav", name="Firma", config={}, feed_priority=9
        )
        storage.sync_events(source_id, [timed_event()], WINDOW_START, WINDOW_END, synced_at=NOW)
        stored = storage.get_events(WINDOW_START, WINDOW_END)
        assert stored[0].feed_priority == 9


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


class TestBusyBlocks:
    def test_upsert_and_list_roundtrip(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        block = BusyBlock(
            source_key="3|uid-1|2026-07-10T16:00:00+00:00",
            google_event_id="gevt-abc",
            start=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
            end=datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
            all_day=False,
        )
        storage.upsert_busy_block(block, updated_at=datetime(2026, 7, 3, tzinfo=UTC))
        stored = storage.list_busy_blocks()
        assert stored == [block]
        assert storage.count_busy_blocks() == 1

    def test_upsert_updates_existing_key(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        key = "3|uid-1|2026-07-10T16:00:00+00:00"
        storage.upsert_busy_block(
            BusyBlock(key, "gevt-1", datetime(2026, 7, 10, 18, tzinfo=UTC),
                      datetime(2026, 7, 10, 19, tzinfo=UTC), False),
            updated_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
        storage.upsert_busy_block(
            BusyBlock(key, "gevt-1", datetime(2026, 7, 10, 20, tzinfo=UTC),
                      datetime(2026, 7, 10, 21, tzinfo=UTC), False),
            updated_at=datetime(2026, 7, 4, tzinfo=UTC),
        )
        stored = storage.list_busy_blocks()
        assert len(stored) == 1
        assert stored[0].start == datetime(2026, 7, 10, 20, tzinfo=UTC)

    def test_all_day_block_roundtrip(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        block = BusyBlock(
            source_key="3|uid-2|2026-07-12",
            google_event_id="gevt-day",
            start=date(2026, 7, 12),
            end=date(2026, 7, 13),
            all_day=True,
        )
        storage.upsert_busy_block(block, updated_at=datetime(2026, 7, 3, tzinfo=UTC))
        assert storage.list_busy_blocks() == [block]

    def test_delete_busy_block(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        key = "3|uid-1|2026-07-10T16:00:00+00:00"
        storage.upsert_busy_block(
            BusyBlock(key, "gevt-1", datetime(2026, 7, 10, 18, tzinfo=UTC),
                      datetime(2026, 7, 10, 19, tzinfo=UTC), False),
            updated_at=datetime(2026, 7, 3, tzinfo=UTC),
        )
        storage.delete_busy_block(key)
        assert storage.list_busy_blocks() == []
        assert storage.count_busy_blocks() == 0

    def test_blocks_survive_reopening(self, tmp_path: Path) -> None:
        block = BusyBlock(
            source_key="3|uid-1|2026-07-10T16:00:00+00:00",
            google_event_id="gevt-1",
            start=datetime(2026, 7, 10, 18, tzinfo=UTC),
            end=datetime(2026, 7, 10, 19, tzinfo=UTC),
            all_day=False,
        )
        make_storage(tmp_path).upsert_busy_block(
            block, updated_at=datetime(2026, 7, 3, tzinfo=UTC)
        )
        assert make_storage(tmp_path).list_busy_blocks() == [block]


def audit_entry(
    ts: str,
    *,
    direction: str = "in",
    scope: str = "Marina",
    action: str = "added",
    title: str = "Termin",
    event_start: str | None = "2026-07-10T16:00:00+00:00",
) -> AuditEntry:
    return AuditEntry(
        ts=ts,
        direction=direction,
        scope=scope,
        action=action,
        title=title,
        event_start=event_start,
    )


class TestAuditLog:
    def test_add_and_get_returns_newest_first(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_audit_entries(
            [
                audit_entry("2026-07-10T10:00:00+00:00", title="A"),
                audit_entry("2026-07-12T10:00:00+00:00", title="C"),
                audit_entry("2026-07-11T10:00:00+00:00", title="B"),
            ]
        )
        got = storage.get_audit_entries("2026-07-01T00:00:00+00:00")
        assert [e.title for e in got] == ["C", "B", "A"]

    def test_empty_batch_is_a_noop(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_audit_entries([])
        assert storage.get_audit_entries("2026-07-01T00:00:00+00:00") == []

    def test_since_ts_filters_out_older_entries(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_audit_entries(
            [
                audit_entry("2026-06-01T10:00:00+00:00", title="old"),
                audit_entry("2026-07-11T10:00:00+00:00", title="new"),
            ]
        )
        got = storage.get_audit_entries("2026-07-01T00:00:00+00:00")
        assert [e.title for e in got] == ["new"]

    def test_limit_caps_the_result(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_audit_entries(
            [audit_entry(f"2026-07-{day:02d}T10:00:00+00:00") for day in range(1, 11)]
        )
        got = storage.get_audit_entries("2026-07-01T00:00:00+00:00", limit=3)
        assert len(got) == 3
        # Newest first: the three highest timestamps.
        assert [e.ts for e in got] == [
            "2026-07-10T10:00:00+00:00",
            "2026-07-09T10:00:00+00:00",
            "2026-07-08T10:00:00+00:00",
        ]

    def test_prune_drops_entries_before_cutoff(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        storage.add_audit_entries(
            [
                audit_entry("2026-06-01T10:00:00+00:00", title="old"),
                audit_entry("2026-07-11T10:00:00+00:00", title="keep"),
            ]
        )
        removed = storage.prune_audit_log("2026-07-01T00:00:00+00:00")
        assert removed == 1
        remaining = storage.get_audit_entries("2026-01-01T00:00:00+00:00")
        assert [e.title for e in remaining] == ["keep"]

    def test_round_trip_preserves_all_fields(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        entry = AuditEntry(
            ts="2026-07-11T10:00:00+00:00",
            direction="out",
            scope="Xalt (Busy MV)",
            action="removed",
            title="Busy MV",
            event_start="2026-07-11",
            details=None,
        )
        storage.add_audit_entries([entry])
        got = storage.get_audit_entries("2026-07-01T00:00:00+00:00")
        assert got == [entry]

    def test_retention_default_is_four_weeks(self) -> None:
        assert AUDIT_RETENTION_DAYS == 28


class TestSyncEventsDiff:
    def test_new_events_are_reported_as_added(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        changes = storage.sync_events(
            source_id, [timed_event(uid="a")], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        assert [(c.action, c.title) for c in changes] == [("added", "Termin")]

    def test_unchanged_resync_reports_no_diff(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        events = [timed_event(uid="a"), timed_event(uid="b")]
        storage.sync_events(source_id, events, WINDOW_START, WINDOW_END, synced_at=NOW)
        # Second identical sync: nothing changed, so no change-log noise.
        changes = storage.sync_events(
            source_id, events, WINDOW_START, WINDOW_END, synced_at=NOW
        )
        assert changes == []

    def test_title_change_is_reported_as_updated(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(uid="a", title="Alt")], WINDOW_START, WINDOW_END,
            synced_at=NOW,
        )
        changes = storage.sync_events(
            source_id, [timed_event(uid="a", title="Neu")], WINDOW_START, WINDOW_END,
            synced_at=NOW,
        )
        assert [(c.action, c.title) for c in changes] == [("updated", "Neu")]

    def test_location_change_is_reported_as_updated(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(uid="a", location=None)], WINDOW_START, WINDOW_END,
            synced_at=NOW,
        )
        changes = storage.sync_events(
            source_id, [timed_event(uid="a", location="München")], WINDOW_START,
            WINDOW_END, synced_at=NOW,
        )
        assert [c.action for c in changes] == ["updated"]

    def test_vanished_in_window_event_is_reported_as_removed(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.sync_events(
            source_id, [timed_event(uid="a", title="Weg")], WINDOW_START, WINDOW_END,
            synced_at=NOW,
        )
        changes = storage.sync_events(
            source_id, [], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        assert [(c.action, c.title) for c in changes] == [("removed", "Weg")]

    def test_time_shift_is_removed_plus_added(self, tmp_path: Path) -> None:
        storage = make_storage(tmp_path)
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        old = timed_event(
            uid="a",
            start=datetime(2026, 7, 10, 8, tzinfo=UTC),
            end=datetime(2026, 7, 10, 9, tzinfo=UTC),
        )
        storage.sync_events(source_id, [old], WINDOW_START, WINDOW_END, synced_at=NOW)
        shifted = timed_event(
            uid="a",
            start=datetime(2026, 7, 10, 10, tzinfo=UTC),
            end=datetime(2026, 7, 10, 11, tzinfo=UTC),
        )
        changes = storage.sync_events(
            source_id, [shifted], WINDOW_START, WINDOW_END, synced_at=NOW
        )
        assert sorted(c.action for c in changes) == ["added", "removed"]
