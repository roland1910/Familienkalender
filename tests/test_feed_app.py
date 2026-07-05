"""Tests for the standalone feed app (app.feed_app).

The ICS feed gets its own minimal ASGI app tree bound to a dedicated
port (container 8100, host 8098) that is deliberately exposed to the
internet — so this app must contain ONLY the feed route and carry its
own hardening: method restriction, security headers, per-IP and global
rate limits and a lockout after repeated wrong-token attempts. All
rate-limit tests use an injectable fake clock.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.feed_app import (
    FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT,
    GLOBAL_MAX_REQUESTS_PER_MINUTE,
    LOCKOUT_SECONDS,
    MAX_REQUESTS_PER_IP_PER_MINUTE,
    MAX_TRACKED_IPS,
    RateLimiter,
    create_feed_app,
)
from app.models import CalendarEvent
from app.settings import ensure_feed_token, get_feed_token, rotate_feed_token
from app.storage import Storage, default_db_path

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)

# The feed port is internet-facing: clients arrive from arbitrary addresses.
CLIENT_IP = "203.0.113.7"
OTHER_CLIENT_IP = "198.51.100.23"


class FakeClock:
    """Injectable monotonic clock for deterministic rate-limit tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def feed_app(storage: Storage, clock: FakeClock):
    return create_feed_app(clock=clock)


@pytest.fixture
def client(feed_app) -> TestClient:
    return TestClient(feed_app, client=(CLIENT_IP, 50000))


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

    def test_non_ascii_token_is_404_not_500(
        self, client: TestClient, storage: Storage
    ) -> None:
        # secrets.compare_digest raises TypeError on non-ASCII str input;
        # the route must answer 404 like any other wrong token instead of
        # letting that become an unhandled 500 for arbitrary clients.
        ensure_feed_token(storage)
        assert client.get("/feed/ä.ics").status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/api/health",
            "/api/events",
            "/api/admin/settings",
            "/admin",
            "/static/js/main.js",
            "/docs",
            "/openapi.json",
            "/feed",
            "/feed/token.ics/extra",
        ],
    )
    def test_nothing_but_the_feed_route_exists(
        self, client: TestClient, storage: Storage, path: str
    ) -> None:
        # The whole point of the separate app tree: even with a valid
        # token configured, no other path is served on this port.
        ensure_feed_token(storage)
        response = client.get(path)
        assert response.status_code == 404
        assert "VCALENDAR" not in response.text

    def test_head_request_is_allowed(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        response = client.head(f"/feed/{token}.ics")
        assert response.status_code == 200
        assert response.content == b""

    @pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
    def test_other_methods_are_405(
        self, client: TestClient, storage: Storage, method: str
    ) -> None:
        token = ensure_feed_token(storage)
        response = getattr(client, method)(f"/feed/{token}.ics")
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD"


class TestSecurityHeaders:
    EXPECTED: ClassVar[dict[str, str]] = {
        "strict-transport-security": "max-age=31536000",
        "x-content-type-options": "nosniff",
        "cache-control": "private, max-age=300",
        "referrer-policy": "no-referrer",
    }

    def _assert_headers(self, response) -> None:
        for name, value in self.EXPECTED.items():
            assert response.headers.get(name) == value, name

    def test_success_response_carries_the_headers(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        self._assert_headers(client.get(f"/feed/{token}.ics"))

    def test_404_response_carries_the_headers(
        self, client: TestClient, storage: Storage
    ) -> None:
        ensure_feed_token(storage)
        self._assert_headers(client.get("/feed/falsch.ics"))

    def test_405_response_carries_the_headers(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        self._assert_headers(client.post(f"/feed/{token}.ics"))


class TestPerIPRateLimit:
    def test_requests_within_the_limit_pass(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(MAX_REQUESTS_PER_IP_PER_MINUTE):
            assert client.get(f"/feed/{token}.ics").status_code == 200

    def test_request_above_the_limit_is_429_with_retry_after(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(MAX_REQUESTS_PER_IP_PER_MINUTE):
            client.get(f"/feed/{token}.ics")
        response = client.get(f"/feed/{token}.ics")
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1

    def test_limit_window_slides(
        self, client: TestClient, storage: Storage, clock: FakeClock
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(MAX_REQUESTS_PER_IP_PER_MINUTE):
            client.get(f"/feed/{token}.ics")
        assert client.get(f"/feed/{token}.ics").status_code == 429
        clock.advance(61)
        assert client.get(f"/feed/{token}.ics").status_code == 200

    def test_limits_are_tracked_per_ip(self, feed_app, storage: Storage) -> None:
        token = ensure_feed_token(storage)
        first = TestClient(feed_app, client=(CLIENT_IP, 50000))
        second = TestClient(feed_app, client=(OTHER_CLIENT_IP, 50000))
        for _ in range(MAX_REQUESTS_PER_IP_PER_MINUTE):
            first.get(f"/feed/{token}.ics")
        assert first.get(f"/feed/{token}.ics").status_code == 429
        assert second.get(f"/feed/{token}.ics").status_code == 200


class TestFailedTokenLockout:
    def test_lockout_after_repeated_failures_blocks_even_a_valid_token(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT):
            assert client.get("/feed/geraten.ics").status_code == 404
        # The lockout is absolute: a brute-force run cannot confirm a hit.
        response = client.get(f"/feed/{token}.ics")
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1

    def test_lockout_expires_after_the_lockout_window(
        self, client: TestClient, storage: Storage, clock: FakeClock
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT):
            client.get("/feed/geraten.ics")
        assert client.get(f"/feed/{token}.ics").status_code == 429
        clock.advance(LOCKOUT_SECONDS + 1)
        assert client.get(f"/feed/{token}.ics").status_code == 200

    def test_fewer_failures_than_the_threshold_do_not_lock(
        self, client: TestClient, storage: Storage
    ) -> None:
        token = ensure_feed_token(storage)
        for _ in range(FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT - 1):
            client.get("/feed/geraten.ics")
        assert client.get(f"/feed/{token}.ics").status_code == 200

    def test_lockout_log_mentions_the_ip_but_never_the_token(
        self,
        client: TestClient,
        storage: Storage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        token = ensure_feed_token(storage)
        with caplog.at_level("INFO", logger="app.feed_app"):
            for _ in range(FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT):
                client.get(f"/feed/{token[:-1]}x.ics")
            client.get(f"/feed/{token}.ics")
        assert CLIENT_IP in caplog.text
        assert token not in caplog.text
        assert token[:-1] + "x" not in caplog.text


class TestRateLimiterUnit:
    def test_global_backstop_blocks_across_ips(self, clock: FakeClock) -> None:
        limiter = RateLimiter(clock=clock)
        for i in range(GLOBAL_MAX_REQUESTS_PER_MINUTE):
            assert limiter.check(f"10.0.{i // 256}.{i % 256}") is None
        assert limiter.check("192.0.2.1") is not None

    def test_global_backstop_window_slides(self, clock: FakeClock) -> None:
        limiter = RateLimiter(clock=clock)
        for i in range(GLOBAL_MAX_REQUESTS_PER_MINUTE):
            limiter.check(f"10.0.{i // 256}.{i % 256}")
        assert limiter.check("192.0.2.1") is not None
        clock.advance(61)
        assert limiter.check("192.0.2.1") is None

    def test_tracked_ips_stay_bounded(self, clock: FakeClock) -> None:
        # Memory DoS guard: many distinct client IPs must not grow the
        # limiter's bookkeeping without bound.
        limiter = RateLimiter(clock=clock)
        for i in range(MAX_TRACKED_IPS + 200):
            limiter.check(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
            clock.advance(61)  # keep the global backstop out of the way
        assert limiter.tracked_ip_count <= MAX_TRACKED_IPS
