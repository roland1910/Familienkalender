"""Tests for the feed route (GET /feed/{token}.ics) and its token auth.

Subscription clients (ICSx5/DAVx5 on Android) cannot authenticate against
Home Assistant, so the feed is reachable past ingress via the mapped host
port. The URL token is the sole auth there; the IP allowlist exceptions
are covered in tests/test_ip_allowlist.py.
"""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import CalendarEvent
from app.settings import ensure_feed_token, get_feed_token, rotate_feed_token
from app.storage import Storage, default_db_path

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)

# A client IP that is deliberately NOT on the allowlist (LAN phone).
LAN_CLIENT = ("192.168.1.42", 50000)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


@pytest.fixture
def client(storage: Storage) -> TestClient:
    return TestClient(app, client=LAN_CLIENT)


def seed_filtered_event(storage: Storage) -> None:
    source_id = storage.add_source(
        type="caldav", name="Firma", config={}, display_mode="filtered", shortcode="RX"
    )
    storage.sync_events(
        source_id,
        [
            CalendarEvent(
                uid="work-evening",
                title="Kundentermin",
                start=datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN),
                end=datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN),
                all_day=False,
            )
        ],
        WINDOW_START,
        WINDOW_END,
        synced_at=NOW,
    )


class TestFeedTokenSettings:
    def test_ensure_creates_a_token_once(self, storage: Storage) -> None:
        assert get_feed_token(storage) is None
        token = ensure_feed_token(storage)
        assert len(token) >= 32
        assert ensure_feed_token(storage) == token  # idempotent
        assert get_feed_token(storage) == token

    def test_rotate_invalidates_the_old_token(self, storage: Storage) -> None:
        old = ensure_feed_token(storage)
        new = rotate_feed_token(storage)
        assert new != old
        assert get_feed_token(storage) == new


class TestFeedRoute:
    def test_valid_token_returns_the_calendar(
        self, client: TestClient, storage: Storage
    ) -> None:
        seed_filtered_event(storage)
        token = ensure_feed_token(storage)
        response = client.get(f"/feed/{token}.ics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        assert "BEGIN:VCALENDAR" in response.text
        assert "RX Kundentermin" in response.text

    def test_wrong_token_is_404(self, client: TestClient, storage: Storage) -> None:
        ensure_feed_token(storage)
        assert client.get("/feed/falsches-token.ics").status_code == 404

    def test_without_configured_token_every_request_is_404(
        self, client: TestClient, storage: Storage
    ) -> None:
        # Nobody opened the admin page yet -> no token exists -> no access.
        assert client.get("/feed/irgendwas.ics").status_code == 404

    def test_rotated_token_invalidates_the_old_url(
        self, client: TestClient, storage: Storage
    ) -> None:
        old = ensure_feed_token(storage)
        new = rotate_feed_token(storage)
        assert client.get(f"/feed/{old}.ics").status_code == 404
        assert client.get(f"/feed/{new}.ics").status_code == 200

    def test_non_ascii_token_is_404_not_500(self, storage: Storage) -> None:
        # secrets.compare_digest raises TypeError on non-ASCII str input;
        # the route must reject it with 404 (like any other wrong token)
        # instead of letting that turn into an unhandled 500. Exercised via
        # an allowlisted client so the route itself is under test, not the
        # IP allowlist's own (ASCII-only) path pattern (see
        # tests/test_ip_allowlist.py).
        ensure_feed_token(storage)
        ingress_client = TestClient(app, client=("172.30.32.2", 50000))
        response = ingress_client.get("/feed/ä.ics")
        assert response.status_code == 404
