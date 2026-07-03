"""SQLite persistence for calendar sources and synced events.

Uses the sqlite3 standard library directly: the schema is two small tables,
there are no relations beyond one foreign key, and the add-on runs on a
Raspberry Pi for a single household — an ORM (or SQLAlchemy Core) would add
a dependency without buying anything here.

Datetime encoding: timed events are stored as ISO 8601 strings normalized
to UTC; all-day events are stored as plain ISO dates. The ``all_day``
column decides how to decode.
"""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from app.models import (
    DISPLAY_MODES,
    SOURCE_TYPES,
    CalendarEvent,
    Source,
    StoredEvent,
    as_local_datetime,
)

DB_FILENAME = "familienkalender.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    display_mode TEXT NOT NULL DEFAULT 'full',
    last_sync_at TEXT,
    last_sync_error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    uid TEXT NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    last_synced TEXT NOT NULL,
    UNIQUE (source_id, uid, start)
);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
"""


def resolve_data_dir() -> Path:
    """Directory for all persistent data.

    Configurable via DATA_DIR; defaults to /data inside the add-on container
    and ./data for local development.
    """
    env_value = os.environ.get("DATA_DIR")
    if env_value:
        return Path(env_value)
    container_dir = Path("/data")
    if container_dir.is_dir():
        return container_dir
    return Path("data")


def default_db_path() -> Path:
    return resolve_data_dir() / DB_FILENAME


def _encode_moment(value: datetime | date) -> str:
    """Encode a start/end value for storage (timed → UTC ISO, all-day → date)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def _decode_moment(raw: str, all_day: bool) -> datetime | date:
    if all_day:
        return date.fromisoformat(raw)
    return datetime.fromisoformat(raw)


def _row_to_event(row: sqlite3.Row) -> CalendarEvent:
    all_day = bool(row["all_day"])
    return CalendarEvent(
        uid=row["uid"],
        title=row["title"],
        start=_decode_moment(row["start"], all_day),
        end=_decode_moment(row["end"], all_day),
        all_day=all_day,
        location=row["location"],
    )


def _row_to_source(row: sqlite3.Row) -> Source:
    last_sync_at = row["last_sync_at"]
    return Source(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        config=json.loads(row["config"]),
        enabled=bool(row["enabled"]),
        display_mode=row["display_mode"],
        last_sync_at=datetime.fromisoformat(last_sync_at) if last_sync_at else None,
        last_sync_error=row["last_sync_error"],
    )


class Storage:
    """All database access goes through this class.

    Every operation opens a short-lived connection: the load is tiny
    (one household, sync every five minutes) and this sidesteps any
    cross-thread connection sharing issues with the async web app.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Wait up to 5s for a concurrent writer instead of failing with
        # "database is locked" (periodic sync vs. manual sync/API access).
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- sources ---------------------------------------------------------

    def add_source(
        self,
        *,
        type: str,
        name: str,
        config: dict,
        enabled: bool = True,
        display_mode: str = "full",
    ) -> int:
        if type not in SOURCE_TYPES:
            raise ValueError(f"unknown source type: {type!r}")
        if display_mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {display_mode!r}")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sources (type, name, config, enabled, display_mode)"
                " VALUES (?, ?, ?, ?, ?)",
                (type, name, json.dumps(config), int(enabled), display_mode),
            )
            return int(cursor.lastrowid or 0)

    def list_sources(self) -> list[Source]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return [_row_to_source(row) for row in rows]

    def update_sync_status(
        self, source_id: int, *, synced_at: datetime, error: str | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sources SET last_sync_at = ?, last_sync_error = ? WHERE id = ?",
                (synced_at.astimezone(UTC).isoformat(), error, source_id),
            )

    # -- events ----------------------------------------------------------

    def sync_events(
        self,
        source_id: int,
        events: list[CalendarEvent],
        window_start: datetime,
        window_end: datetime,
        *,
        synced_at: datetime,
    ) -> None:
        """Upsert the fetched events and delete window events that vanished.

        ``events`` is the complete fetch result for [window_start, window_end);
        stored events of this source starting inside the window that are not in
        the result set were deleted upstream and are removed here too. Events
        outside the window are left alone.
        """
        fetched_keys = {(event.uid, _encode_moment(event.start)) for event in events}
        synced_at_raw = synced_at.astimezone(UTC).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, uid, start, all_day FROM events WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            stale_ids = [
                row["id"]
                for row in rows
                if (row["uid"], row["start"]) not in fetched_keys
                and window_start
                <= as_local_datetime(_decode_moment(row["start"], bool(row["all_day"])))
                < window_end
            ]
            if stale_ids:
                conn.executemany(
                    "DELETE FROM events WHERE id = ?", [(item,) for item in stale_ids]
                )
            conn.executemany(
                "INSERT INTO events"
                " (source_id, uid, title, start, end, all_day, location, last_synced)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, uid, start) DO UPDATE SET"
                " title = excluded.title, end = excluded.end,"
                " all_day = excluded.all_day, location = excluded.location,"
                " last_synced = excluded.last_synced",
                [
                    (
                        source_id,
                        event.uid,
                        event.title,
                        _encode_moment(event.start),
                        _encode_moment(event.end),
                        int(event.all_day),
                        event.location,
                        synced_at_raw,
                    )
                    for event in events
                ],
            )

    def get_events(self, range_start: datetime, range_end: datetime) -> list[StoredEvent]:
        """All stored events overlapping [range_start, range_end), sorted by start."""
        # Range filtering happens in Python, not in SQL: the start/end
        # columns mix UTC ISO datetimes (timed events) with plain ISO dates
        # (all-day events), so a lexicographic SQL comparison would be wrong
        # across the two encodings. The table is tiny (one household).
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.*, s.name AS source_name, s.display_mode AS source_display_mode"
                " FROM events e JOIN sources s ON s.id = e.source_id"
            ).fetchall()
        result = []
        for row in rows:
            event = _row_to_event(row)
            if (
                as_local_datetime(event.start) < range_end
                and as_local_datetime(event.end) > range_start
            ):
                result.append(
                    StoredEvent(
                        source_id=row["source_id"],
                        source_name=row["source_name"],
                        display_mode=row["source_display_mode"],
                        event=event,
                    )
                )
        result.sort(key=lambda item: as_local_datetime(item.event.start))
        return result
