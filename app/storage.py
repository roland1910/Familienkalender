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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path

from app.models import (
    DISPLAY_MODES,
    MAX_TAGS_PER_DAY,
    SOURCE_TYPES,
    TAG_OPTIONS,
    AuditEntry,
    BusyBlock,
    CalendarEvent,
    EventChange,
    Source,
    StoredEvent,
    TagLimitError,
    UnknownTagError,
    as_local_datetime,
    is_valid_feed_priority,
    is_valid_shortcode,
    is_valid_source_color,
)

DB_FILENAME = "familienkalender.db"

# How long change-log entries are kept before the periodic prune drops them
# (Roland wants "the last 4 weeks"). Shared with the admin API window.
AUDIT_RETENTION_DAYS = 28

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    display_mode TEXT NOT NULL DEFAULT 'full',
    last_sync_at TEXT,
    last_sync_error TEXT,
    shortcode TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    include_in_feed INTEGER NOT NULL DEFAULT 0,
    feed_priority INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_tags (
    date TEXT NOT NULL,
    position INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    PRIMARY KEY (date, position)
);
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    mtime REAL NOT NULL,
    shown INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS busy_blocks (
    source_key TEXT PRIMARY KEY,
    google_event_id TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    all_day INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    scope TEXT NOT NULL,
    action TEXT NOT NULL,
    title TEXT NOT NULL,
    event_start TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""

# Emojis allowed in day_tags — everything else is rejected before it ever
# reaches the database (guards against XSS payloads and junk strings).
_ALLOWED_TAG_EMOJIS = frozenset(option.emoji for option in TAG_OPTIONS)


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
        shortcode=row["shortcode"],
        color=row["color"],
        include_in_feed=bool(row["include_in_feed"]),
        feed_priority=row["feed_priority"],
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
        self._ensure_private_db_file()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database created by an older version up to the schema.

        CREATE TABLE IF NOT EXISTS never touches existing tables, so columns
        added later must be retrofitted here.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
        if "shortcode" not in columns:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN shortcode TEXT NOT NULL DEFAULT ''"
            )
        if "color" not in columns:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN color TEXT NOT NULL DEFAULT ''"
            )
        if "include_in_feed" not in columns:
            # Existing installations keep the historical feed content:
            # until this column existed, exactly the filtered sources
            # (Roland's work calendars) fed the ICS feed.
            conn.execute(
                "ALTER TABLE sources ADD COLUMN include_in_feed"
                " INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE sources SET include_in_feed = (display_mode = 'filtered')"
            )
        if "feed_priority" not in columns:
            # De-duplication precedence for the ICS feed; existing sources
            # start at the neutral default (ties broken by source id).
            conn.execute(
                "ALTER TABLE sources ADD COLUMN feed_priority"
                " INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_private_db_file(self) -> None:
        """Make sure the DB file exists with owner-only permissions (0600).

        The database stores source configs including app passwords, so it
        must be private to the app user. The mode is applied atomically at
        creation via os.open (same pattern as token files — no window in
        which a fresh file is world-readable); a pre-existing file from an
        older version is tightened via chmod. SQLite creates journal/WAL
        siblings with the DB file's permissions, so they inherit 0600.
        """
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        self.db_path.chmod(0o600)

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
        shortcode: str = "",
        color: str = "",
        include_in_feed: bool | None = None,
        feed_priority: int = 0,
    ) -> int:
        if type not in SOURCE_TYPES:
            raise ValueError(f"unknown source type: {type!r}")
        if display_mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {display_mode!r}")
        if not is_valid_shortcode(shortcode):
            raise ValueError(f"invalid shortcode: {shortcode!r}")
        if not is_valid_source_color(color):
            raise ValueError(f"invalid source color: {color!r}")
        if not is_valid_feed_priority(feed_priority):
            raise ValueError(f"invalid feed priority: {feed_priority!r}")
        if include_in_feed is None:
            # Historical default: filtered sources (work calendars) feed
            # the ICS subscription, fully displayed ones do not.
            include_in_feed = display_mode == "filtered"
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sources"
                " (type, name, config, enabled, display_mode, shortcode, color,"
                " include_in_feed, feed_priority)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    type,
                    name,
                    json.dumps(config),
                    int(enabled),
                    display_mode,
                    shortcode,
                    color,
                    int(include_in_feed),
                    feed_priority,
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_sources(self) -> list[Source]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return [_row_to_source(row) for row in rows]

    def get_source(self, source_id: int) -> Source | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _row_to_source(row) if row else None

    def update_source(
        self,
        source_id: int,
        *,
        name: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
        display_mode: str | None = None,
        shortcode: str | None = None,
        color: str | None = None,
        include_in_feed: bool | None = None,
        feed_priority: int | None = None,
    ) -> bool:
        """Partially update a source; returns False if it does not exist."""
        if display_mode is not None and display_mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode: {display_mode!r}")
        if shortcode is not None and not is_valid_shortcode(shortcode):
            raise ValueError(f"invalid shortcode: {shortcode!r}")
        if color is not None and not is_valid_source_color(color):
            raise ValueError(f"invalid source color: {color!r}")
        if feed_priority is not None and not is_valid_feed_priority(feed_priority):
            raise ValueError(f"invalid feed priority: {feed_priority!r}")
        assignments: list[str] = []
        values: list = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if config is not None:
            assignments.append("config = ?")
            values.append(json.dumps(config))
        if enabled is not None:
            assignments.append("enabled = ?")
            values.append(int(enabled))
        if display_mode is not None:
            assignments.append("display_mode = ?")
            values.append(display_mode)
        if shortcode is not None:
            assignments.append("shortcode = ?")
            values.append(shortcode)
        if color is not None:
            assignments.append("color = ?")
            values.append(color)
        if include_in_feed is not None:
            assignments.append("include_in_feed = ?")
            values.append(int(include_in_feed))
        if feed_priority is not None:
            assignments.append("feed_priority = ?")
            values.append(feed_priority)
        if not assignments:
            return self.get_source(source_id) is not None
        with self._connect() as conn:
            # The f-string only splices column names from the literals
            # above — every value goes through a ? placeholder, so this
            # is not an injection surface.
            cursor = conn.execute(
                f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?",
                (*values, source_id),
            )
            return cursor.rowcount > 0

    def delete_source(self, source_id: int) -> bool:
        """Delete a source and (via FK cascade) all its events."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            return cursor.rowcount > 0

    def count_events_by_source(self) -> dict[int, int]:
        """Stored event count per source id (0 for sources without events)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.id AS id, COUNT(e.id) AS n"
                " FROM sources s LEFT JOIN events e ON e.source_id = s.id"
                " GROUP BY s.id"
            ).fetchall()
        return {row["id"]: row["n"] for row in rows}

    def update_sync_status(
        self, source_id: int, *, synced_at: datetime, error: str | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sources SET last_sync_at = ?, last_sync_error = ? WHERE id = ?",
                (synced_at.astimezone(UTC).isoformat(), error, source_id),
            )

    # -- settings --------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    # -- day tags --------------------------------------------------------

    def set_day_tags(self, day: date, emojis: list[str]) -> list[str]:
        """Replace the tags of one day with the given emoji list.

        Only whitelisted emojis (TAG_OPTIONS) are accepted, duplicates are
        collapsed (first occurrence wins) and at most MAX_TAGS_PER_DAY tags
        are allowed. Returns the stored list. Raises UnknownTagError or
        TagLimitError on invalid input — before anything is written. This is
        the single place that enforces these rules; callers (the API layer)
        only translate the exception types into HTTP responses.
        """
        deduped = list(dict.fromkeys(emojis))
        for emoji in deduped:
            if emoji not in _ALLOWED_TAG_EMOJIS:
                raise UnknownTagError(f"emoji not in whitelist: {emoji!r}")
        if len(deduped) > MAX_TAGS_PER_DAY:
            raise TagLimitError(f"at most {MAX_TAGS_PER_DAY} tags per day")
        day_iso = day.isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM day_tags WHERE date = ?", (day_iso,))
            conn.executemany(
                "INSERT INTO day_tags (date, position, emoji) VALUES (?, ?, ?)",
                [(day_iso, position, emoji) for position, emoji in enumerate(deduped)],
            )
        return deduped

    def get_day_tags(self, from_date: date, to_date: date) -> dict[str, list[str]]:
        """Tags per ISO date for [from_date, to_date] (inclusive), in set order.

        Only days that actually have tags appear in the result.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, emoji FROM day_tags WHERE date >= ? AND date <= ?"
                " ORDER BY date, position",
                (from_date.isoformat(), to_date.isoformat()),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["date"], []).append(row["emoji"])
        return result

    # -- photos (slideshow) ----------------------------------------------

    def replace_photos(self, photos: list[tuple[str, float]]) -> int:
        """Replace the photo index with the given (path, mtime) pairs.

        Called after a filesystem scan. Rebuilds the table wholesale in a
        single transaction: paths that vanished disappear, new ones are
        added, and the ``shown`` rotation state is reset for everyone (a
        fresh scan starts a fresh rotation cycle). Returns the number of
        rows stored. Duplicate paths in the input are collapsed by the
        UNIQUE constraint via INSERT OR IGNORE.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM photos")
            conn.executemany(
                "INSERT OR IGNORE INTO photos (path, mtime, shown) VALUES (?, ?, 0)",
                photos,
            )
            row = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()
        return int(row["n"])

    def list_photo_entries(self) -> list[tuple[str, float]]:
        """Every indexed photo as a (path, mtime) pair.

        Used by the scan to carry entries of *currently unavailable*
        directories over into the rebuilt index (see slideshow._scan_sync):
        an unmounted share must not silently shrink the index.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT path, mtime FROM photos").fetchall()
        return [(row["path"], float(row["mtime"])) for row in rows]

    def count_photos(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM photos").fetchone()
        return int(row["n"])

    def get_photo_path(self, photo_id: int) -> str | None:
        """The stored filesystem path for a photo id, or None if unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT path FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
        return row["path"] if row else None

    def delete_photo(self, photo_id: int) -> None:
        """Drop a single photo from the index (e.g. its file vanished)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))

    def pick_next_photo(self, rng_random: Callable[[], float] | None = None) -> dict | None:
        """Pick a random not-yet-shown photo, mark it shown, return it.

        Rotation with memory: only photos with ``shown = 0`` are eligible.
        When none are left the whole index is reset to ``shown = 0`` and a
        pick is retried once — one full pass through all photos before any
        repeats, surviving restarts because the state lives in the DB.

        ``rng_random`` is an injectable ``random.random``-style callable
        (returns a float in [0, 1)) so tests get deterministic ordering;
        production passes ``None`` and SQLite's own ``RANDOM()`` is used.
        Returns ``{"id", "name", "path"}`` (name = basename for display,
        path = stored filesystem path for server-side metadata lookups —
        the API layer must never expose it) or None when the index is empty.
        """
        with self._connect() as conn:
            row = self._pick_unshown(conn, rng_random)
            if row is None:
                conn.execute("UPDATE photos SET shown = 0")
                row = self._pick_unshown(conn, rng_random)
            if row is None:
                return None
            conn.execute("UPDATE photos SET shown = 1 WHERE id = ?", (row["id"],))
            return {
                "id": row["id"],
                "name": Path(row["path"]).name,
                "path": row["path"],
            }

    @staticmethod
    def _pick_unshown(
        conn: sqlite3.Connection, rng_random: Callable[[], float] | None
    ) -> sqlite3.Row | None:
        """One random unshown photo row, or None if all are shown."""
        if rng_random is None:
            return conn.execute(
                "SELECT id, path FROM photos WHERE shown = 0 ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
        rows = conn.execute(
            "SELECT id, path FROM photos WHERE shown = 0 ORDER BY id"
        ).fetchall()
        if not rows:
            return None
        return rows[int(rng_random() * len(rows)) % len(rows)]

    # -- events ----------------------------------------------------------

    def sync_events(
        self,
        source_id: int,
        events: list[CalendarEvent],
        window_start: datetime,
        window_end: datetime,
        *,
        synced_at: datetime,
    ) -> list[EventChange]:
        """Upsert the fetched events, delete vanished ones, return the diff.

        ``events`` is the complete fetch result for [window_start, window_end);
        stored events of this source starting inside the window that are not in
        the result set were deleted upstream and are removed here too. Events
        outside the window are left alone.

        The returned diff (against the CURRENT database state, computed from the
        rows and events already in hand — no extra query) drives the change log:
        - ``added``: a fetched event whose key (uid|start) was not stored before.
        - ``removed``: a stored, in-window event no longer present upstream.
        - ``updated``: a still-present event whose title/end/all_day/location
          changed. A pure time shift is not an update — it changes the key and
          thus shows as removed(old)+added(new). When nothing changed the diff
          is empty (no change-log noise); a first sync against an already
          populated DB (e.g. right after a redeploy) therefore logs nothing.
        """
        fetched_keys = {(event.uid, _encode_moment(event.start)) for event in events}
        synced_at_raw = synced_at.astimezone(UTC).isoformat()
        changes: list[EventChange] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, uid, title, start, end, all_day, location"
                " FROM events WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            existing = {(row["uid"], row["start"]): row for row in rows}
            stale_ids = []
            for row in rows:
                if (row["uid"], row["start"]) in fetched_keys:
                    continue
                if (
                    window_start
                    <= as_local_datetime(
                        _decode_moment(row["start"], bool(row["all_day"]))
                    )
                    < window_end
                ):
                    stale_ids.append(row["id"])
                    changes.append(
                        EventChange("removed", row["title"], row["start"])
                    )
            for event in events:
                encoded_start = _encode_moment(event.start)
                row = existing.get((event.uid, encoded_start))
                if row is None:
                    changes.append(EventChange("added", event.title, encoded_start))
                elif (
                    row["title"] != event.title
                    or row["end"] != _encode_moment(event.end)
                    or bool(row["all_day"]) != event.all_day
                    or row["location"] != event.location
                ):
                    changes.append(EventChange("updated", event.title, encoded_start))
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
        return changes

    # -- audit log (Änderungsprotokoll) ----------------------------------

    def add_audit_entries(self, entries: list[AuditEntry]) -> None:
        """Append change-log entries in one batch (no-op on an empty list)."""
        if not entries:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO audit_log"
                " (ts, direction, scope, action, title, event_start, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        entry.ts,
                        entry.direction,
                        entry.scope,
                        entry.action,
                        entry.title,
                        entry.event_start,
                        entry.details,
                    )
                    for entry in entries
                ],
            )

    def get_audit_entries(self, since_ts: str, *, limit: int = 1000) -> list[AuditEntry]:
        """Change-log entries with ``ts >= since_ts``, newest first, capped.

        ``ts`` is an ISO-8601 UTC string; all entries are written in the same
        UTC format, so the lexicographic comparison matches chronological order.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, direction, scope, action, title, event_start, details"
                " FROM audit_log WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
                (since_ts, limit),
            ).fetchall()
        return [_row_to_audit_entry(row) for row in rows]

    def prune_audit_log(self, before_ts: str) -> int:
        """Delete change-log entries older than ``before_ts``; return the count."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM audit_log WHERE ts < ?", (before_ts,))
            return cursor.rowcount

    def get_events(self, range_start: datetime, range_end: datetime) -> list[StoredEvent]:
        """All stored events of enabled sources overlapping [range_start, range_end).

        Sorted by start. Events of disabled sources stay in the database
        (a re-enabled source's history is not lost) but never surface here —
        this is the single choke point behind both /api/events and the ICS
        feed, so disabling a source hides it from both consistently.

        Range filtering happens in Python, not in SQL: the start/end
        columns mix UTC ISO datetimes (timed events) with plain ISO dates
        (all-day events), so a lexicographic SQL comparison would be wrong
        across the two encodings. The table is tiny (one household).
        The enabled filter has no such issue and is applied in SQL.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.*, s.name AS source_name, s.display_mode AS source_display_mode,"
                " s.shortcode AS source_shortcode, s.color AS source_color,"
                " s.include_in_feed AS source_include_in_feed,"
                " s.feed_priority AS source_feed_priority"
                " FROM events e JOIN sources s ON s.id = e.source_id"
                " WHERE s.enabled = 1"
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
                        shortcode=row["source_shortcode"],
                        color=row["source_color"],
                        include_in_feed=bool(row["source_include_in_feed"]),
                        feed_priority=row["source_feed_priority"],
                    )
                )
        result.sort(key=lambda item: as_local_datetime(item.event.start))
        return result

    # -- busy blocks (one-way "Busy MV" sync into Xalt) ------------------

    def list_busy_blocks(self) -> list[BusyBlock]:
        """All persisted busy-block mappings (source_key → Google event id)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_key, google_event_id, start, end, all_day"
                " FROM busy_blocks ORDER BY source_key"
            ).fetchall()
        return [_row_to_busy_block(row) for row in rows]

    def upsert_busy_block(self, block: BusyBlock, *, updated_at: datetime) -> None:
        """Insert or update the mapping for one source key."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO busy_blocks"
                " (source_key, google_event_id, start, end, all_day, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_key) DO UPDATE SET"
                " google_event_id = excluded.google_event_id,"
                " start = excluded.start, end = excluded.end,"
                " all_day = excluded.all_day, updated_at = excluded.updated_at",
                (
                    block.source_key,
                    block.google_event_id,
                    _encode_moment(block.start),
                    _encode_moment(block.end),
                    int(block.all_day),
                    updated_at.astimezone(UTC).isoformat(),
                ),
            )

    def delete_busy_block(self, source_key: str) -> None:
        """Drop the mapping for one source key (the block was deleted upstream)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM busy_blocks WHERE source_key = ?", (source_key,)
            )

    def count_busy_blocks(self) -> int:
        """Number of active busy-block mappings (for the admin status view)."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM busy_blocks").fetchone()
        return int(row["n"])

    def clear_busy_blocks(self) -> None:
        """Drop ALL busy-block mappings (a deliberate local state reset).

        Used when the write token is disconnected: the Google-side "Busy MV"
        blocks are intentionally left in place (see delete_google_write_token
        in app.admin), but the local mapping must not survive an account
        switch — otherwise a later reconnect could patch/delete google_event_ids
        that now belong to a different Google account's calendar.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM busy_blocks")


def _row_to_audit_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        ts=row["ts"],
        direction=row["direction"],
        scope=row["scope"],
        action=row["action"],
        title=row["title"],
        event_start=row["event_start"],
        details=row["details"],
    )


def _row_to_busy_block(row: sqlite3.Row) -> BusyBlock:
    all_day = bool(row["all_day"])
    return BusyBlock(
        source_key=row["source_key"],
        google_event_id=row["google_event_id"],
        start=_decode_moment(row["start"], all_day),
        end=_decode_moment(row["end"], all_day),
        all_day=all_day,
    )


# Unbounded cache (equivalent to lru_cache(maxsize=None)): there is
# exactly one DATA_DIR in production and one per test; a bounded cache
# could evict (and later recreate) a Storage that is still in use, which
# buys nothing and costs re-initialization.
@cache
def _storage_for(db_path: Path) -> Storage:
    return Storage(db_path)


def get_storage() -> Storage:
    """Storage for the current DATA_DIR (env is re-read so tests can vary it)."""
    return _storage_for(default_db_path())
