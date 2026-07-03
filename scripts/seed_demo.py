"""Seed demo sources and events into DATA_DIR for local development and E2E tests.

Usage:
    python scripts/seed_demo.py            # seeds into DATA_DIR (default ./data)

The demo data mirrors the real setup: a fully displayed family calendar
(Marina) plus two filtered work calendars (Kunde, Firma). It includes
umlauts, multi-day events, an overflow day with many events, and events
with hostile XSS payload titles for manual verification that the frontend
renders titles as text only.
"""

import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath

# Allow running as a plain script: `python scripts/seed_demo.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import LOCAL_TZ, CalendarEvent
from app.storage import DB_FILENAME, Storage

# The add-on's production data directory inside the container.
PROD_DATA_DIR = PurePosixPath("/data")


def ensure_seed_target_allowed(data_dir: Path) -> None:
    """Refuse to seed the production data dir unless explicitly allowed.

    /data holds the real family calendar inside the add-on container;
    accidentally running the seed there would mix demo events into it.
    The comparison uses the POSIX form so it is platform-independent
    (and thereby unit-testable on Windows, where /data does not exist).
    """
    if PurePosixPath(Path(data_dir).as_posix()) != PROD_DATA_DIR:
        return
    if os.environ.get("FAMILIENKALENDER_ALLOW_PROD_SEED"):
        return
    raise SystemExit(
        "Abbruch: Ziel ist das Produktions-Datenverzeichnis /data. "
        "Demo-Daten dort nur mit FAMILIENKALENDER_ALLOW_PROD_SEED=1 anlegen."
    )

# (name, source type, display mode) — mirrors the real family setup.
DEMO_SOURCES = (
    ("Marina", "google", "full"),
    ("Kunde", "google", "filtered"),
    ("Firma", "caldav", "filtered"),
)


def _timed(uid: str, title: str, day: date, start: time, end: time, *,
           location: str | None = None) -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title=title,
        start=datetime.combine(day, start, tzinfo=LOCAL_TZ),
        end=datetime.combine(day, end, tzinfo=LOCAL_TZ),
        all_day=False,
        location=location,
    )


def _all_day(uid: str, title: str, start: date, days: int) -> CalendarEvent:
    """All-day event over ``days`` calendar days (exclusive iCalendar end)."""
    return CalendarEvent(
        uid=uid, title=title, start=start, end=start + timedelta(days=days), all_day=True
    )


def _marina_events(today: date) -> list[CalendarEvent]:
    next_month_first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    overflow_day = today + timedelta(days=3)
    events = [
        _timed("demo-marina-zahnarzt", "Zahnarzt Emil", today, time(15), time(16),
               location="Praxis Dr. Müller"),
        _timed("demo-marina-fussball", "Fußball-Training", today - timedelta(days=7),
               time(17), time(18, 30), location="Turnhalle Süd"),
        _timed("demo-marina-fussball", "Fußball-Training", today + timedelta(days=7),
               time(17), time(18, 30), location="Turnhalle Süd"),
        _timed("demo-marina-sommerfest", "Kita-Sommerfest", today + timedelta(days=2),
               time(15, 30), time(18)),
        _all_day("demo-marina-besuch", "Oma & Opa zu Besuch", today + timedelta(days=5), 3),
        _all_day("demo-marina-geburtstag", "Geburtstag Tante Käthe", next_month_first, 1),
        # Hostile titles: stored as-is, the frontend must render them as text.
        _timed("demo-marina-xss-img", '<img src=x onerror=alert("xss")> Böser Termin',
               today + timedelta(days=1), time(10), time(11)),
        _timed("demo-marina-xss-script", '<script>alert("xss")</script> Skript-Termin',
               today + timedelta(days=1), time(12), time(13)),
    ]
    # One crowded day so the month cell overflow ("+N weitere") appears.
    for index in range(6):
        events.append(
            _timed(
                f"demo-marina-voll-{index + 1}",
                f"Demo-Termin {index + 1}",
                overflow_day,
                time(8 + index),
                time(8 + index, 45),
            )
        )
    return events


def _kunde_events(today: date) -> list[CalendarEvent]:
    return [
        # Reaches past the 17:00 boundary — shown.
        _timed("demo-kunde-termin", "Kundentermin München", today + timedelta(days=1),
               time(16), time(19), location="München"),
        # Plain daytime meeting — stored, but hidden by the family filter.
        _timed("demo-kunde-weekly", "Weekly Sync", today + timedelta(days=1),
               time(10), time(11)),
        # Multi-day business trip — shown.
        CalendarEvent(
            uid="demo-kunde-messe",
            title="Messe Berlin",
            start=datetime.combine(today + timedelta(days=10), time(9), tzinfo=LOCAL_TZ),
            end=datetime.combine(today + timedelta(days=12), time(17), tzinfo=LOCAL_TZ),
            all_day=False,
            location="Berlin",
        ),
    ]


def _firma_events(today: date) -> list[CalendarEvent]:
    return [
        _timed("demo-firma-bereitschaft", "Bereitschaft Nachtschicht",
               today + timedelta(days=4), time(20), time(23)),
        # Hidden by the family filter.
        _timed("demo-firma-meeting", "Team-Meeting", today + timedelta(days=4),
               time(9), time(10)),
        _all_day("demo-firma-fortbildung", "Fortbildung Hamburg",
                 today + timedelta(days=14), 2),
    ]


def seed_demo(data_dir: Path, *, today: date | None = None) -> dict[str, int]:
    """Create demo sources and events in ``data_dir``; idempotent.

    Returns a mapping of source name to source id.
    """
    ensure_seed_target_allowed(data_dir)
    today = today or date.today()
    storage = Storage(Path(data_dir) / DB_FILENAME)

    existing = {source.name: source.id for source in storage.list_sources()}
    source_ids: dict[str, int] = {}
    for name, source_type, display_mode in DEMO_SOURCES:
        if name in existing:
            source_ids[name] = existing[name]
        else:
            source_ids[name] = storage.add_source(
                type=source_type, name=name, config={}, display_mode=display_mode
            )

    events_by_source = {
        "Marina": _marina_events(today),
        "Kunde": _kunde_events(today),
        "Firma": _firma_events(today),
    }
    window_start = datetime.combine(today - timedelta(days=60), time.min, tzinfo=LOCAL_TZ)
    window_end = datetime.combine(today + timedelta(days=90), time.min, tzinfo=LOCAL_TZ)
    synced_at = datetime.now(tz=LOCAL_TZ)
    for name, events in events_by_source.items():
        storage.sync_events(
            source_ids[name], events, window_start, window_end, synced_at=synced_at
        )
        storage.update_sync_status(source_ids[name], synced_at=synced_at, error=None)
    return source_ids


def main() -> None:
    from app.storage import resolve_data_dir

    data_dir = resolve_data_dir()
    # Announce the target before anything is written, so an aborted or
    # interrupted run still tells the user where it was about to write.
    print(f"Ziel-Datenverzeichnis: {data_dir}")
    source_ids = seed_demo(data_dir)
    print(f"Demo-Daten angelegt in {data_dir} (Quellen: {source_ids})")


if __name__ == "__main__":
    main()
