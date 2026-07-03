"""Integration test against the real Nextcloud server.

Not part of the default test run: requires secrets.local.json (gitignored)
and network access to the server. Run explicitly with:

    pytest -m integration -s
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.sources.caldav import fetch_events, list_calendars

SECRETS_FILE = Path(__file__).parent.parent / "secrets.local.json"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not SECRETS_FILE.exists(), reason="secrets.local.json fehlt"),
]


@pytest.fixture
def nextcloud_config() -> dict:
    secrets = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))["nextcloud"]
    return {
        "url": secrets["url"],
        "username": secrets["username"],
        "app_password": secrets["app_password"],
    }


async def test_lists_real_calendars(nextcloud_config: dict) -> None:
    calendars = await list_calendars(nextcloud_config)
    print(f"\nGefundene Kalender: {[calendar['name'] for calendar in calendars]}")
    assert calendars, "expected at least one event calendar"
    for calendar in calendars:
        assert calendar["url"].startswith(nextcloud_config["url"])


async def test_fetches_events_from_real_calendars(nextcloud_config: dict) -> None:
    calendars = await list_calendars(nextcloud_config)
    window_start = datetime.now(UTC) - timedelta(days=7)
    window_end = datetime.now(UTC) + timedelta(days=90)
    total = 0
    for calendar in calendars:
        config = {**nextcloud_config, "calendar_url": calendar["url"]}
        events = await fetch_events(config, window_start, window_end)
        total += len(events)
        print(f"Kalender '{calendar['name']}': {len(events)} Events im Fenster")
        for event in events[:5]:
            print(f"  - {event.start} {event.title!r} (all_day={event.all_day})")
        for event in events:
            assert event.uid
            assert event.end_as_datetime() >= event.start_as_datetime()
    print(f"Gesamt: {total} Events")
