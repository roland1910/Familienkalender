"""Tests for the request auth module (app/auth.py) and the admin gate.

The Supervisor ingress proxy forwards the authenticated HA user in the
X-Remote-User-* headers (no admin flag header exists); admin group
membership is resolved via the HA WebSocket command config/auth/list.
These tests cover the header parsing, the WS lookup protocol, the
caching, and the resulting 403 gate on /api/admin/* and /admin —
including that the non-admin endpoints (calendar, tags, power) stay
open.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app

HA_INGRESS_IP = "172.30.32.2"
USER_ID_HEADER = "X-Remote-User-Id"

ADMIN_USER = {
    "id": "admin-user-id",
    "username": "roland",
    "name": "Roland",
    "is_owner": False,
    "is_active": True,
    "system_generated": False,
    "group_ids": ["system-admin"],
}
NORMAL_USER = {
    "id": "normal-user-id",
    "username": "marina",
    "name": "Marina",
    "is_owner": False,
    "is_active": True,
    "system_generated": False,
    "group_ids": ["system-users"],
}


@pytest.fixture(autouse=True)
def clean_auth_state(monkeypatch: pytest.MonkeyPatch):
    """Fresh cache and a hermetic environment for every test."""
    for name in ("HA_WS_URL", "HA_API_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    auth.reset_cache()
    yield
    auth.reset_cache()


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def localhost_client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def ingress_client() -> TestClient:
    return TestClient(app, client=(HA_INGRESS_IP, 50000))


def fake_lookup(monkeypatch: pytest.MonkeyPatch, admin_ids: set[str]) -> list[int]:
    """Replace the WS lookup with a canned result; returns a call counter."""
    calls: list[int] = []

    async def _fake() -> frozenset[str]:
        calls.append(1)
        return frozenset(admin_ids)

    monkeypatch.setattr(auth, "_fetch_admin_user_ids", _fake)
    return calls


def failing_lookup(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def _fail() -> frozenset[str]:
        calls.append(1)
        raise auth.AdminLookupError("kaputt")

    monkeypatch.setattr(auth, "_fetch_admin_user_ids", _fail)
    return calls


class TestUserIsAdmin:
    def test_active_member_of_admin_group_is_admin(self) -> None:
        assert auth._user_is_admin(ADMIN_USER) is True

    def test_owner_without_group_ids_is_admin(self) -> None:
        owner = {"id": "owner-id", "is_owner": True, "is_active": True}
        assert auth._user_is_admin(owner) is True

    def test_inactive_admin_is_not_admin(self) -> None:
        assert auth._user_is_admin({**ADMIN_USER, "is_active": False}) is False

    def test_normal_user_is_not_admin(self) -> None:
        assert auth._user_is_admin(NORMAL_USER) is False

    def test_missing_fields_mean_not_admin(self) -> None:
        assert auth._user_is_admin({"id": "x"}) is False

    def test_non_list_group_ids_mean_not_admin_not_a_crash(self) -> None:
        # An unexpected shape for group_ids (anything but a list/None) must
        # not raise — treat it like "no admin group membership" instead.
        user = {"id": "x", "is_active": True, "is_owner": False, "group_ids": 42}
        assert auth._user_is_admin(user) is False


class FakeWebSocket:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = [json.dumps(message) for message in incoming]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        return self.incoming.pop(0)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


class FakeConnect:
    """Stand-in for websockets' connect() async context manager."""

    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket
        self.url: str | None = None

    def __call__(self, url: str, **_kwargs) -> "FakeConnect":
        self.url = url
        return self

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *_exc) -> bool:
        return False


class TestFetchAdminUserIds:
    @pytest.mark.anyio
    async def test_happy_path_speaks_the_ha_ws_protocol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required", "ha_version": "2026.7"},
                {"type": "auth_ok", "ha_version": "2026.7"},
                {"id": 1, "type": "result", "success": True,
                 "result": [ADMIN_USER, NORMAL_USER]},
            ]
        )
        connect = FakeConnect(websocket)
        monkeypatch.setattr(auth, "connect", connect)

        admin_ids = await auth._fetch_admin_user_ids()

        assert admin_ids == frozenset({"admin-user-id"})
        assert connect.url == auth.DEFAULT_HA_WS_URL
        assert websocket.sent[0] == {"type": "auth", "access_token": "super-secret"}
        assert websocket.sent[1] == {"id": 1, "type": "config/auth/list"}

    @pytest.mark.anyio
    async def test_interleaved_event_messages_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 99, "type": "event", "event": {}},
                {"id": 1, "type": "result", "success": True, "result": [ADMIN_USER]},
            ]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        assert await auth._fetch_admin_user_ids() == frozenset({"admin-user-id"})

    @pytest.mark.anyio
    async def test_rejected_auth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "wrong")
        websocket = FakeWebSocket(
            [{"type": "auth_required"}, {"type": "auth_invalid", "message": "nope"}]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        with pytest.raises(auth.AdminLookupError):
            await auth._fetch_admin_user_ids()

    @pytest.mark.anyio
    async def test_unsuccessful_command_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": False, "error": {}},
            ]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        with pytest.raises(auth.AdminLookupError):
            await auth._fetch_admin_user_ids()

    @pytest.mark.anyio
    async def test_overridden_ws_url_requires_explicit_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The SUPERVISOR_TOKEN belongs to the default supervisor URL only;
        # an overridden URL without HA_API_TOKEN must fail instead of
        # leaking the token to a different host.
        monkeypatch.setenv("HA_WS_URL", "ws://127.0.0.1:1/ws")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        with pytest.raises(auth.AdminLookupError):
            await auth._fetch_admin_user_ids()

    @pytest.mark.anyio
    async def test_admin_user_without_id_is_skipped_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An admin-shaped dict with no "id" field is an unexpected server
        # response, not a reason to 500 — skip it like any other malformed
        # entry instead of raising KeyError.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        admin_without_id = {k: v for k, v in ADMIN_USER.items() if k != "id"}
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": [admin_without_id, ADMIN_USER],
                },
            ]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        assert await auth._fetch_admin_user_ids() == frozenset({"admin-user-id"})

    @pytest.mark.anyio
    async def test_result_as_object_instead_of_array_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "result" is expected to be a list of user dicts; a bare object
        # (or any other non-list) must be a clean AdminLookupError, not a
        # TypeError from iterating/indexing the wrong shape.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True, "result": ADMIN_USER},
            ]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        with pytest.raises(auth.AdminLookupError):
            await auth._fetch_admin_user_ids()

    @pytest.mark.anyio
    async def test_mixed_result_list_skips_non_dict_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": True,
                    "result": ["not-a-dict", 42, None, ADMIN_USER],
                },
            ]
        )
        monkeypatch.setattr(auth, "connect", FakeConnect(websocket))
        assert await auth._fetch_admin_user_ids() == frozenset({"admin-user-id"})


class TestIsUserAdminCaching:
    @pytest.mark.anyio
    async def test_lookup_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_lookup(monkeypatch, {"admin-user-id"})
        assert await auth.is_user_admin("admin-user-id") is True
        assert await auth.is_user_admin("normal-user-id") is False
        assert len(calls) == 1

    @pytest.mark.anyio
    async def test_failed_lookup_is_non_admin_and_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = failing_lookup(monkeypatch)
        assert await auth.is_user_admin("admin-user-id") is False
        assert await auth.is_user_admin("admin-user-id") is False
        assert len(calls) == 1

    @pytest.mark.anyio
    async def test_cache_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = fake_lookup(monkeypatch, {"admin-user-id"})
        fake_now = [100.0]
        monkeypatch.setattr(auth, "_now", lambda: fake_now[0])
        assert await auth.is_user_admin("admin-user-id") is True
        fake_now[0] += auth.ADMIN_CACHE_TTL_SECONDS + 1
        assert await auth.is_user_admin("admin-user-id") is True
        assert len(calls) == 2

    @pytest.mark.anyio
    async def test_concurrent_misses_trigger_only_one_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A parallel cache miss (e.g. several page loads right after
        # startup or a cache expiry) must share a single WS lookup instead
        # of each request opening its own connection — single-flight,
        # mirroring app.power's _fetch_lock.
        calls: list[int] = []

        async def _slow_fetch() -> frozenset[str]:
            calls.append(1)
            await asyncio.sleep(0)
            return frozenset({"admin-user-id"})

        monkeypatch.setattr(auth, "_fetch_admin_user_ids", _slow_fetch)
        results = await asyncio.gather(
            *(auth.is_user_admin("admin-user-id") for _ in range(5))
        )
        assert calls == [1]
        assert results == [True] * 5


class TestApiMe:
    def test_localhost_without_user_header_is_admin(self) -> None:
        # Local development and the E2E suite run without ingress; the real
        # ingress proxy always sets the user header, so this fallback never
        # applies in production.
        response = localhost_client().get("/api/me")
        assert response.status_code == 200
        assert response.json() == {"is_admin": True}

    def test_ingress_without_user_header_is_not_admin(self) -> None:
        response = ingress_client().get("/api/me")
        assert response.status_code == 200
        assert response.json() == {"is_admin": False}

    def test_ingress_with_admin_user_header_is_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_lookup(monkeypatch, {"admin-user-id"})
        response = ingress_client().get(
            "/api/me", headers={USER_ID_HEADER: "admin-user-id"}
        )
        assert response.json() == {"is_admin": True}

    def test_ingress_with_normal_user_header_is_not_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_lookup(monkeypatch, {"admin-user-id"})
        response = ingress_client().get(
            "/api/me", headers={USER_ID_HEADER: "normal-user-id"}
        )
        assert response.json() == {"is_admin": False}

    def test_user_header_wins_over_localhost_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A request that carries a user id is judged by that user id, even
        # from 127.0.0.1 — the fallback exists only for header-less local
        # traffic (dev server, container healthcheck).
        fake_lookup(monkeypatch, {"admin-user-id"})
        response = localhost_client().get(
            "/api/me", headers={USER_ID_HEADER: "normal-user-id"}
        )
        assert response.json() == {"is_admin": False}


class TestLocalhostFallbackDisabledInProduction:
    """SUPERVISOR_TOKEN is only ever set inside the add-on container — a
    reliable production marker. The localhost fallback must not apply
    there, even though the IP allowlist would otherwise let 127.0.0.1
    through: the container healthcheck talks to the app from inside the
    same container, so a mistaken production deployment must not grant
    it (or anything else reaching 127.0.0.1) admin rights.
    """

    def test_localhost_without_header_is_not_admin_when_supervisor_token_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        response = localhost_client().get("/api/me")
        assert response.json() == {"is_admin": False}

    def test_localhost_without_header_is_admin_when_no_supervisor_token(self) -> None:
        # Unchanged dev/E2E behaviour: no SUPERVISOR_TOKEN in the
        # environment means this is not the production container.
        response = localhost_client().get("/api/me")
        assert response.json() == {"is_admin": True}


class TestAdminGate:
    def test_admin_api_rejects_non_admin_with_german_message(self) -> None:
        client = ingress_client()
        for method, url in (
            ("GET", "/api/admin/settings"),
            ("GET", "/api/admin/sources"),
            ("POST", "/api/admin/sources"),
            ("PUT", "/api/admin/settings"),
            ("PUT", "/api/admin/settings/power"),
        ):
            response = client.request(method, url)
            assert response.status_code == 403, f"{method} {url}"
            assert response.json() == {"detail": "Nur für Administratoren."}

    def test_admin_api_allows_admin_user_via_header(
        self, monkeypatch: pytest.MonkeyPatch, data_dir: Path
    ) -> None:
        fake_lookup(monkeypatch, {"admin-user-id"})
        response = ingress_client().get(
            "/api/admin/settings", headers={USER_ID_HEADER: "admin-user-id"}
        )
        assert response.status_code == 200

    def test_admin_api_allows_localhost_without_header(self, data_dir: Path) -> None:
        response = localhost_client().get("/api/admin/settings")
        assert response.status_code == 200

    def test_admin_page_shows_german_403_page_to_non_admin(self) -> None:
        response = ingress_client().get("/admin")
        assert response.status_code == 403
        assert "text/html" in response.headers["content-type"]
        assert "Nur für Administratoren." in response.text
        assert "Zurück zum Kalender" in response.text

    def test_admin_page_served_to_admin(self, data_dir: Path) -> None:
        response = localhost_client().get("/admin")
        assert response.status_code == 200
        assert "Verwaltung" in response.text


class TestNonAdminEndpointsStayOpen:
    """Calendar, tags and power stay usable for normal HA users."""

    def test_events_endpoint_is_open(self, data_dir: Path) -> None:
        response = ingress_client().get(
            "/api/events", params={"from": "2026-07-01", "to": "2026-07-31"}
        )
        assert response.status_code == 200

    def test_tags_endpoints_are_open(self, data_dir: Path) -> None:
        client = ingress_client()
        response = client.get(
            "/api/tags", params={"from": "2026-07-01", "to": "2026-07-31"}
        )
        assert response.status_code == 200
        assert client.get("/api/tags/options").status_code == 200
        put = client.put("/api/tags/2026-07-04", json={"emojis": ["🎉"]})
        assert put.status_code == 200

    def test_power_endpoint_is_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app import power

        async def fake_snapshot() -> dict:
            return {"devices": []}

        monkeypatch.setattr(power, "_fetch_snapshot", fake_snapshot)
        response = ingress_client().get("/api/power")
        assert response.status_code == 200

    def test_index_health_and_static_are_open(self) -> None:
        client = ingress_client()
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/static/js/main.js").status_code == 200
