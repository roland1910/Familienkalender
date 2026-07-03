"""Tests for the admin API (/api/admin/*).

The admin endpoints run behind HA ingress plus the IP allowlist like the
rest of the app. Central rule: secrets (app passwords, client secret,
tokens) must never appear in any API response — asserted negatively here.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.google_oauth import GoogleOAuthError
from app.main import app
from app.models import CalendarEvent
from app.sources.google import pending_token_path, token_path
from app.storage import Storage, default_db_path

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)

CALDAV_CONFIG = {
    "url": "https://cloud.example.com",
    "username": "roland",
    "app_password": "sehr-geheim",
    "calendar_url": "https://cloud.example.com/remote.php/dav/calendars/roland/firma/",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Storage(default_db_path())


class TestSettingsEndpoints:
    def test_get_settings_defaults(self, client: TestClient, storage: Storage) -> None:
        response = client.get("/api/admin/settings")
        assert response.status_code == 200
        payload = response.json()
        assert payload["evening_boundary"] == "17:00"
        assert payload["google_credentials"]["configured"] is False

    def test_put_evening_boundary_persists(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.put("/api/admin/settings", json={"evening_boundary": "18:30"})
        assert response.status_code == 200
        assert storage.get_setting("evening_boundary") == "18:30"
        assert client.get("/api/admin/settings").json()["evening_boundary"] == "18:30"

    def test_put_invalid_boundary_is_rejected(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.put("/api/admin/settings", json={"evening_boundary": "25:99"})
        assert response.status_code == 400
        assert "Uhrzeit" in response.json()["detail"]

    def test_put_google_credentials_and_masked_status(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.put(
            "/api/admin/settings/google",
            json={"client_id": "12345678900.apps.googleusercontent.com",
                  "client_secret": "super-geheimes-secret"},
        )
        assert response.status_code == 200
        assert "super-geheimes-secret" not in response.text

        status = client.get("/api/admin/settings")
        payload = status.json()["google_credentials"]
        assert payload["configured"] is True
        # Masked: recognizable prefix, but not the full value.
        assert payload["client_id_masked"].startswith("12345678")
        assert "12345678900.apps.googleusercontent.com" not in status.text
        assert "super-geheimes-secret" not in status.text

    def test_put_google_credentials_requires_both_fields(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.put(
            "/api/admin/settings/google", json={"client_id": "x", "client_secret": ""}
        )
        assert response.status_code == 400

    def test_put_google_credentials_with_placeholder_keeps_secret(
        self, client: TestClient, storage: Storage
    ) -> None:
        # The admin UI never sees the stored secret — sending the mask
        # placeholder back must keep it (e.g. when fixing the client id).
        client.put(
            "/api/admin/settings/google",
            json={"client_id": "cid-alt", "client_secret": "geheim"},
        )
        response = client.put(
            "/api/admin/settings/google",
            json={"client_id": "cid-neu", "client_secret": "***"},
        )
        assert response.status_code == 200
        assert storage.get_setting("google_client_id") == "cid-neu"
        assert storage.get_setting("google_client_secret") == "geheim"

    def test_put_google_credentials_placeholder_without_stored_secret_is_400(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.put(
            "/api/admin/settings/google",
            json={"client_id": "cid", "client_secret": "***"},
        )
        assert response.status_code == 400


class TestSourcesList:
    def test_lists_sources_with_status_and_counts(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(
            type="caldav", name="Firma", config=CALDAV_CONFIG, display_mode="filtered"
        )
        storage.sync_events(
            source_id,
            [
                CalendarEvent(
                    uid="e1",
                    title="Termin",
                    start=datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN),
                    end=datetime(2026, 7, 10, 19, 0, tzinfo=BERLIN),
                    all_day=False,
                )
            ],
            WINDOW_START,
            WINDOW_END,
            synced_at=NOW,
        )
        storage.update_sync_status(source_id, synced_at=NOW, error=None)

        response = client.get("/api/admin/sources")
        assert response.status_code == 200
        source = response.json()["sources"][0]
        assert source["name"] == "Firma"
        assert source["type"] == "caldav"
        assert source["display_mode"] == "filtered"
        assert source["event_count"] == 1
        assert source["last_sync_at"] is not None
        assert source["last_sync_error"] is None

    def test_config_never_contains_the_app_password(
        self, client: TestClient, storage: Storage
    ) -> None:
        storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        response = client.get("/api/admin/sources")
        assert "sehr-geheim" not in response.text
        config = response.json()["sources"][0]["config"]
        assert config["url"] == CALDAV_CONFIG["url"]
        assert config["username"] == "roland"
        assert config["app_password"] == "***"


class TestCreateCaldavSource:
    def test_creates_source_with_valid_config(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.post(
            "/api/admin/sources",
            json={
                "type": "caldav",
                "name": "Firma",
                "display_mode": "filtered",
                "config": CALDAV_CONFIG,
            },
        )
        assert response.status_code == 201
        assert "sehr-geheim" not in response.text
        sources = storage.list_sources()
        assert len(sources) == 1
        assert sources[0].config == CALDAV_CONFIG  # secrets stored, never returned

    def test_rejects_forbidden_calendar_url(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        config = {**CALDAV_CONFIG, "calendar_url": "http://172.30.32.2/dav/"}
        response = client.post(
            "/api/admin/sources",
            json={"type": "caldav", "name": "X", "display_mode": "full", "config": config},
        )
        assert response.status_code == 400
        assert storage.list_sources() == []

    def test_rejects_missing_fields(self, client: TestClient, storage: Storage) -> None:
        response = client.post(
            "/api/admin/sources",
            json={"type": "caldav", "name": "", "display_mode": "full",
                  "config": {"url": "https://x"}},
        )
        assert response.status_code == 400

    def test_rejects_name_longer_than_200_characters(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.post(
            "/api/admin/sources",
            json={"type": "caldav", "name": "x" * 201, "display_mode": "full",
                  "config": CALDAV_CONFIG},
        )
        assert response.status_code == 400
        assert "200" in response.json()["detail"]
        assert storage.list_sources() == []

    def test_unknown_config_keys_are_discarded(
        self, client: TestClient, storage: Storage
    ) -> None:
        config = {**CALDAV_CONFIG, "unbekannt": "wert", "__proto__": "x"}
        response = client.post(
            "/api/admin/sources",
            json={"type": "caldav", "name": "Firma", "display_mode": "full",
                  "config": config},
        )
        assert response.status_code == 201
        assert storage.list_sources()[0].config == CALDAV_CONFIG

    def test_rejects_unknown_type_and_mode(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.post(
            "/api/admin/sources",
            json={"type": "outlook", "name": "X", "display_mode": "full", "config": {}},
        )
        assert response.status_code == 400
        response = client.post(
            "/api/admin/sources",
            json={"type": "caldav", "name": "X", "display_mode": "halb",
                  "config": CALDAV_CONFIG},
        )
        assert response.status_code == 400


class TestUpdateAndDeleteSource:
    def test_patch_name_mode_enabled(self, client: TestClient, storage: Storage) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        response = client.patch(
            f"/api/admin/sources/{source_id}",
            json={"name": "Firma neu", "display_mode": "filtered", "enabled": False},
        )
        assert response.status_code == 200
        source = storage.get_source(source_id)
        assert source.name == "Firma neu"
        assert source.display_mode == "filtered"
        assert source.enabled is False

    def test_patch_config_keeps_stored_password_when_masked(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        new_config = {**CALDAV_CONFIG, "app_password": "***", "username": "neu"}
        response = client.patch(
            f"/api/admin/sources/{source_id}", json={"config": new_config}
        )
        assert response.status_code == 200
        stored = storage.get_source(source_id).config
        assert stored["username"] == "neu"
        assert stored["app_password"] == "sehr-geheim"  # unchanged

    def test_patch_config_replaces_password_when_given(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        new_config = {**CALDAV_CONFIG, "app_password": "neues-passwort"}
        client.patch(f"/api/admin/sources/{source_id}", json={"config": new_config})
        assert storage.get_source(source_id).config["app_password"] == "neues-passwort"

    def test_patch_empty_app_password_is_422_and_keeps_secret(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        new_config = {**CALDAV_CONFIG, "app_password": ""}
        response = client.patch(
            f"/api/admin/sources/{source_id}", json={"config": new_config}
        )
        assert response.status_code == 422
        assert "App-Passwort" in response.json()["detail"]
        assert storage.get_source(source_id).config["app_password"] == "sehr-geheim"

    def test_patch_name_longer_than_200_is_rejected(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        response = client.patch(
            f"/api/admin/sources/{source_id}", json={"name": "x" * 201}
        )
        assert response.status_code == 400
        assert storage.get_source(source_id).name == "Firma"

    def test_patch_discards_unknown_config_keys(
        self, client: TestClient, storage: Storage
    ) -> None:
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        new_config = {**CALDAV_CONFIG, "app_password": "***", "extra": "weg-damit"}
        response = client.patch(
            f"/api/admin/sources/{source_id}", json={"config": new_config}
        )
        assert response.status_code == 200
        assert storage.get_source(source_id).config == CALDAV_CONFIG

    def test_patch_config_validates_urls(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        source_id = storage.add_source(type="caldav", name="Firma", config=CALDAV_CONFIG)
        bad = {**CALDAV_CONFIG, "calendar_url": "http://169.254.1.1/"}
        response = client.patch(f"/api/admin/sources/{source_id}", json={"config": bad})
        assert response.status_code == 400
        assert storage.get_source(source_id).config == CALDAV_CONFIG

    def test_patch_missing_source_is_404(self, client: TestClient, storage: Storage) -> None:
        assert client.patch("/api/admin/sources/99", json={"name": "X"}).status_code == 404

    def test_delete_source_removes_events_and_google_tokens(
        self, client: TestClient, storage: Storage, tmp_path: Path
    ) -> None:
        source_id = storage.add_source(
            type="google", name="Marina", config={"calendar_id": "m@example.com"}
        )
        tokens_file = token_path(source_id)
        tokens_file.write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
        response = client.delete(f"/api/admin/sources/{source_id}")
        assert response.status_code == 200
        assert storage.list_sources() == []
        assert not tokens_file.exists()

    def test_delete_missing_source_is_404(self, client: TestClient, storage: Storage) -> None:
        assert client.delete("/api/admin/sources/99").status_code == 404


class TestCaldavCalendarsEndpoint:
    def test_lists_calendars_via_client(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}

        async def fake_list_calendars(config, *, client=None):
            seen["config"] = config
            return [{"name": "Firma", "url": "https://cloud.example.com/cal/firma/"}]

        monkeypatch.setattr("app.admin.caldav.list_calendars", fake_list_calendars)
        response = client.post(
            "/api/admin/caldav/calendars",
            json={"url": "https://cloud.example.com", "username": "roland",
                  "app_password": "sehr-geheim"},
        )
        assert response.status_code == 200
        assert response.json()["calendars"] == [
            {"name": "Firma", "url": "https://cloud.example.com/cal/firma/"}
        ]
        assert seen["config"]["app_password"] == "sehr-geheim"
        assert "sehr-geheim" not in response.text

    def test_forbidden_url_is_rejected_before_any_request(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FAMILIENKALENDER_ALLOW_HTTP", raising=False)
        calls: list = []

        async def fake_list_calendars(config, *, client=None):
            calls.append(config)
            return []

        monkeypatch.setattr("app.admin.caldav.list_calendars", fake_list_calendars)
        response = client.post(
            "/api/admin/caldav/calendars",
            json={"url": "http://172.30.32.2", "username": "u", "app_password": "p"},
        )
        assert response.status_code == 400
        assert calls == []

    def test_connection_error_gives_sanitized_german_message(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_list_calendars(config, *, client=None):
            raise RuntimeError("boom at https://roland:sehr-geheim@cloud.example.com/")

        monkeypatch.setattr("app.admin.caldav.list_calendars", fake_list_calendars)
        response = client.post(
            "/api/admin/caldav/calendars",
            json={"url": "https://cloud.example.com", "username": "roland",
                  "app_password": "sehr-geheim"},
        )
        assert response.status_code == 502
        assert "sehr-geheim" not in response.text
        assert "fehlgeschlagen" in response.json()["detail"]


class TestGoogleFlowEndpoints:
    def _set_credentials(self, client: TestClient) -> None:
        client.put(
            "/api/admin/settings/google",
            json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "cs"},
        )

    def test_auth_url_requires_credentials(
        self, client: TestClient, storage: Storage
    ) -> None:
        response = client.post("/api/admin/google/auth-url")
        assert response.status_code == 400
        assert "Client-ID" in response.json()["detail"]

    def test_auth_url_is_returned(self, client: TestClient, storage: Storage) -> None:
        self._set_credentials(client)
        response = client.post("/api/admin/google/auth-url")
        assert response.status_code == 200
        auth_url = response.json()["auth_url"]
        assert auth_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "cid.apps.googleusercontent.com" in auth_url
        assert "cs" not in auth_url.replace("cid.apps", "")  # secret never in URL

    def test_connect_exchanges_code_and_lists_calendars(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_credentials(client)
        seen: dict = {}

        async def fake_exchange(code, *, client_id, client_secret, client=None):
            seen["code"] = code
            seen["client_id"] = client_id
            seen["client_secret"] = client_secret
            return {"client_id": client_id, "client_secret": client_secret,
                    "refresh_token": "rt-1", "access_token": "at-1",
                    "access_token_expires_at": "2026-07-03T13:00:00+00:00"}

        async def fake_calendar_list(access_token, *, client=None):
            seen["access_token"] = access_token
            return [{"id": "m@example.com", "name": "Marina"}]

        monkeypatch.setattr("app.admin.google_oauth.exchange_code", fake_exchange)
        monkeypatch.setattr("app.admin.google_oauth.fetch_calendar_list", fake_calendar_list)

        response = client.post(
            "/api/admin/google/connect",
            json={"code": "http://localhost:1/?code=4%2F0AbCdEf&scope=x"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["calendars"] == [{"id": "m@example.com", "name": "Marina"}]
        assert seen["code"] == "4/0AbCdEf"  # extraction happened
        assert seen["access_token"] == "at-1"
        # Tokens are parked in a per-flow pending file until the source is
        # created; the random flow id is the claim ticket for adoption.
        flow_id = payload["flow_id"]
        assert len(flow_id) >= 16
        pending = json.loads(pending_token_path(flow_id).read_text(encoding="utf-8"))
        assert pending["refresh_token"] == "rt-1"
        # No token material in the response.
        assert "rt-1" not in response.text
        assert "at-1" not in response.text

    def test_connect_maps_oauth_errors_to_400(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_credentials(client)

        async def fake_exchange(code, *, client_id, client_secret, client=None):
            raise GoogleOAuthError("Der Code ist ungültig oder abgelaufen.")

        monkeypatch.setattr("app.admin.google_oauth.exchange_code", fake_exchange)
        response = client.post("/api/admin/google/connect", json={"code": "4/alt"})
        assert response.status_code == 400
        assert "abgelaufen" in response.json()["detail"]

    def test_connect_unexpected_error_gives_sanitized_german_message(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_credentials(client)

        async def fake_exchange(code, *, client_id, client_secret, client=None):
            raise RuntimeError("boom at https://user:geheim@oauth2.googleapis.com/token")

        monkeypatch.setattr("app.admin.google_oauth.exchange_code", fake_exchange)
        response = client.post("/api/admin/google/connect", json={"code": "4/x"})
        assert response.status_code == 502
        assert "geheim" not in response.text
        assert "fehlgeschlagen" in response.json()["detail"]

    def test_connect_with_bad_paste_is_400(
        self, client: TestClient, storage: Storage
    ) -> None:
        self._set_credentials(client)
        response = client.post(
            "/api/admin/google/connect",
            json={"code": "http://localhost:1/?error=access_denied"},
        )
        assert response.status_code == 400

    def _park_pending_tokens(self, flow_id: str = "abcDEF123_-x") -> str:
        pending = pending_token_path(flow_id)
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps({"refresh_token": "rt-1"}), encoding="utf-8")
        return flow_id

    def test_create_google_source_adopts_pending_tokens_via_flow_id(
        self, client: TestClient, storage: Storage
    ) -> None:
        flow_id = self._park_pending_tokens()

        response = client.post(
            "/api/admin/sources",
            json={"type": "google", "name": "Marina", "display_mode": "full",
                  "config": {"calendar_id": "m@example.com"}, "flow_id": flow_id},
        )
        assert response.status_code == 201
        source_id = response.json()["source"]["id"]
        assert not pending_token_path(flow_id).exists()
        tokens = json.loads(token_path(source_id).read_text(encoding="utf-8"))
        assert tokens["refresh_token"] == "rt-1"
        assert "rt-1" not in response.text

    def test_create_google_source_without_flow_id_is_400(
        self, client: TestClient, storage: Storage
    ) -> None:
        self._park_pending_tokens()
        response = client.post(
            "/api/admin/sources",
            json={"type": "google", "name": "Marina", "display_mode": "full",
                  "config": {"calendar_id": "m@example.com"}},
        )
        assert response.status_code == 400
        assert "verbinden" in response.json()["detail"]
        assert storage.list_sources() == []

    def test_create_google_source_with_unknown_flow_id_is_400(
        self, client: TestClient, storage: Storage
    ) -> None:
        self._park_pending_tokens()
        response = client.post(
            "/api/admin/sources",
            json={"type": "google", "name": "Marina", "display_mode": "full",
                  "config": {"calendar_id": "m@example.com"}, "flow_id": "falsche-id"},
        )
        assert response.status_code == 400
        assert storage.list_sources() == []

    def test_create_google_source_with_malicious_flow_id_is_400(
        self, client: TestClient, storage: Storage
    ) -> None:
        # flow_id becomes part of a filename — path traversal must fail.
        response = client.post(
            "/api/admin/sources",
            json={"type": "google", "name": "Marina", "display_mode": "full",
                  "config": {"calendar_id": "m@example.com"},
                  "flow_id": "../../etc/passwd"},
        )
        assert response.status_code == 400
        assert storage.list_sources() == []


class TestGooglePendingLifecycle:
    def _set_credentials(self, client: TestClient) -> None:
        client.put(
            "/api/admin/settings/google",
            json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "cs"},
        )

    def test_delete_pending_flow_removes_the_file(
        self, client: TestClient, storage: Storage
    ) -> None:
        pending = pending_token_path("flow123")
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps({"refresh_token": "rt-1"}), encoding="utf-8")

        response = client.delete("/api/admin/google/pending/flow123")
        assert response.status_code == 200
        assert not pending.exists()

    def test_delete_unknown_pending_flow_is_idempotent(
        self, client: TestClient, storage: Storage
    ) -> None:
        # The wizard reset calls this endpoint unconditionally.
        assert client.delete("/api/admin/google/pending/unbekannt").status_code == 200

    def test_delete_with_malicious_flow_id_is_400(
        self, client: TestClient, storage: Storage, tmp_path: Path
    ) -> None:
        victim = tmp_path / "opfer.json"
        victim.write_text("{}", encoding="utf-8")
        response = client.delete("/api/admin/google/pending/..%2F..%2Fopfer.json")
        assert response.status_code in (400, 404)
        assert victim.exists()

    def test_stale_pending_files_are_cleaned_on_flow_start(
        self, client: TestClient, storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_credentials(client)
        stale = pending_token_path("altcode")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"refresh_token": "rt-alt"}), encoding="utf-8")
        old = (datetime.now(UTC) - timedelta(minutes=30)).timestamp()
        os.utime(stale, (old, old))
        fresh = pending_token_path("frisch")
        fresh.write_text(json.dumps({"refresh_token": "rt-neu"}), encoding="utf-8")

        response = client.post("/api/admin/google/auth-url")
        assert response.status_code == 200
        assert not stale.exists()
        assert fresh.exists()
