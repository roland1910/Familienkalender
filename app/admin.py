"""Admin API (/api/admin/*): sources CRUD, settings, OAuth connect.

Reachability: like every other route, these endpoints sit behind HA
ingress plus the client-IP allowlist middleware — no separate auth layer.

Secret handling: app passwords and the Google client secret are stored
(source config JSON / settings table) but never returned by any endpoint.
Configs in responses carry a mask placeholder instead of the password;
a PATCH sending the placeholder back keeps the stored secret.
"""

import logging
import re
import secrets
from datetime import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import google_oauth, power, settings
from app.models import DISPLAY_MODES, SOURCE_TYPES, Source
from app.sanitize import sanitize_error
from app.settings import get_evening_boundary
from app.sources import caldav, google
from app.storage import get_storage
from app.url_validation import SourceURLError, validate_source_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")

# Placeholder returned instead of stored secrets; a PATCH echoing it back
# means "keep the stored value".
SECRET_MASK = "***"

# Config keys holding secrets (never returned, mask-aware on update).
_SECRET_CONFIG_KEYS = ("app_password",)

# Config keys that are fetch targets and must pass validate_source_url.
_URL_CONFIG_KEYS = ("url", "calendar_url")

# Allowed config keys per source type. Unknown keys are discarded so the
# raw JSON from the client can never persist (and later expose) anything
# the sync engine does not consume.
_CONFIG_KEYS_BY_TYPE = {
    "caldav": ("url", "username", "app_password", "calendar_url"),
    "google": ("calendar_id",),
}

# Source names are display strings; the cap keeps hostile input from
# bloating the DB and the admin UI.
MAX_NAME_LENGTH = 200

# Power-view device list: HA entity ids are lowercase domain.object_id.
# Only sensors make sense here — anything else is a configuration error.
_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MAX_POWER_DEVICES = 30
MAX_POWER_DEVICE_NAME_LENGTH = 100


class SettingsUpdate(BaseModel):
    evening_boundary: str


class PowerDeviceIn(BaseModel):
    entity_id: str
    name: str


class PowerDevicesUpdate(BaseModel):
    devices: list[PowerDeviceIn] = Field(max_length=MAX_POWER_DEVICES)


class GoogleCredentials(BaseModel):
    client_id: str
    client_secret: str


class SourceCreate(BaseModel):
    type: str
    name: str
    display_mode: str
    config: dict
    # Claim ticket from POST /google/connect; required for Google sources.
    flow_id: str | None = None


class SourceUpdate(BaseModel):
    name: str | None = None
    display_mode: str | None = None
    enabled: bool | None = None
    config: dict | None = None


class CaldavProbe(BaseModel):
    url: str
    username: str
    app_password: str


class GoogleConnect(BaseModel):
    code: str


def _mask_client_id(client_id: str) -> str:
    """Recognizable prefix so the admin can tell which client is configured."""
    return client_id[:8] + "…" if len(client_id) > 8 else client_id


def _masked_config(config: dict) -> dict:
    masked = dict(config)
    for key in _SECRET_CONFIG_KEYS:
        if masked.get(key):
            masked[key] = SECRET_MASK
    return masked


def _validate_display_mode(display_mode: str) -> None:
    if display_mode not in DISPLAY_MODES:
        raise HTTPException(
            status_code=400, detail=f"Unbekannter Anzeigemodus: {display_mode!r}"
        )


def _validated_name(name: str) -> str:
    """Trimmed source name, or 400 for empty/overlong names."""
    stripped = name.strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Der Name darf nicht leer sein.")
    if len(stripped) > MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Der Name darf höchstens {MAX_NAME_LENGTH} Zeichen lang sein.",
        )
    return stripped


def _filtered_config(source_type: str, config: dict) -> dict:
    """The config restricted to the whitelisted keys for this source type."""
    allowed = _CONFIG_KEYS_BY_TYPE[source_type]
    return {key: value for key, value in config.items() if key in allowed}


def _validate_config_urls(config: dict) -> None:
    try:
        for key in _URL_CONFIG_KEYS:
            if config.get(key):
                validate_source_url(config[key])
    except SourceURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _serialize_source(source: Source, event_count: int) -> dict:
    return {
        "id": source.id,
        "type": source.type,
        "name": source.name,
        "enabled": source.enabled,
        "display_mode": source.display_mode,
        "config": _masked_config(source.config),
        "last_sync_at": source.last_sync_at.isoformat() if source.last_sync_at else None,
        "last_sync_error": source.last_sync_error,
        "event_count": event_count,
    }


# -- settings --------------------------------------------------------------


def _settings_payload() -> dict:
    storage = get_storage()
    client_id = storage.get_setting(settings.GOOGLE_CLIENT_ID_KEY)
    client_secret = storage.get_setting(settings.GOOGLE_CLIENT_SECRET_KEY)
    return {
        "evening_boundary": get_evening_boundary(storage).strftime("%H:%M"),
        "google_credentials": {
            "configured": bool(client_id and client_secret),
            "client_id_masked": _mask_client_id(client_id) if client_id else None,
        },
        "power_devices": [
            {"entity_id": device.entity_id, "name": device.name}
            for device in settings.get_power_devices(storage)
        ],
    }


@router.get("/settings")
async def get_settings() -> dict:
    """Current settings — the Google client secret is never included."""
    return _settings_payload()


@router.put("/settings")
async def update_settings(update: SettingsUpdate) -> dict:
    try:
        time.fromisoformat(update.evening_boundary)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Ungültige Uhrzeit — bitte im Format HH:MM angeben."
        ) from exc
    get_storage().set_setting(settings.EVENING_BOUNDARY_KEY, update.evening_boundary)
    return _settings_payload()


@router.put("/settings/google")
async def update_google_credentials(credentials: GoogleCredentials) -> dict:
    client_id = credentials.client_id.strip()
    client_secret = credentials.client_secret.strip()
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400, detail="Client-ID und Client-Secret dürfen nicht leer sein."
        )
    storage = get_storage()
    if client_secret == SECRET_MASK:
        # The mask placeholder means: keep the stored secret (the UI never
        # sees the real value, e.g. when only fixing the client id).
        stored_secret = storage.get_setting(settings.GOOGLE_CLIENT_SECRET_KEY)
        if not stored_secret:
            raise HTTPException(
                status_code=400,
                detail="Es ist noch kein Client-Secret gespeichert — bitte"
                " das echte Secret eingeben.",
            )
        client_secret = stored_secret
    storage.set_setting(settings.GOOGLE_CLIENT_ID_KEY, client_id)
    storage.set_setting(settings.GOOGLE_CLIENT_SECRET_KEY, client_secret)
    return _settings_payload()


@router.put("/settings/power")
async def update_power_devices(update: PowerDevicesUpdate) -> dict:
    """Replace the power-view device list (an empty list is valid)."""
    devices = []
    for item in update.devices:
        entity_id = item.entity_id.strip()
        name = item.name.strip()
        if not _ENTITY_ID_PATTERN.fullmatch(entity_id):
            raise HTTPException(
                status_code=400, detail=f"Ungültige Entity-ID: {item.entity_id!r}"
            )
        if not name:
            raise HTTPException(
                status_code=400, detail="Der Anzeigename darf nicht leer sein."
            )
        if len(name) > MAX_POWER_DEVICE_NAME_LENGTH:
            raise HTTPException(
                status_code=400,
                detail="Der Anzeigename darf höchstens"
                f" {MAX_POWER_DEVICE_NAME_LENGTH} Zeichen lang sein.",
            )
        devices.append(settings.PowerDevice(entity_id, name))
    settings.set_power_devices(get_storage(), devices)
    # The next /api/power request must already reflect the new list.
    power.reset_cache()
    return _settings_payload()


# -- sources ---------------------------------------------------------------


@router.get("/sources")
async def list_sources() -> dict:
    """All sources with sync status and event count, secrets masked."""
    storage = get_storage()
    counts = storage.count_events_by_source()
    return {
        "sources": [
            _serialize_source(source, counts.get(source.id, 0))
            for source in storage.list_sources()
        ]
    }


def _validate_caldav_create(config: dict) -> None:
    missing = [
        key
        for key in ("url", "username", "app_password", "calendar_url")
        if not config.get(key)
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Fehlende Angaben: {', '.join(missing)}",
        )
    _validate_config_urls(config)


def _pending_tokens_or_400(flow_id: str | None) -> Path:
    """The pending token file for a flow id, or 400 with a German message."""
    if not flow_id:
        raise HTTPException(
            status_code=400,
            detail="Bitte zuerst das Google-Konto verbinden (Code einlösen).",
        )
    try:
        pending = google.pending_token_path(flow_id)
    except google.InvalidFlowIdError as exc:
        raise HTTPException(status_code=400, detail="Ungültige Flow-ID.") from exc
    if not pending.exists():
        raise HTTPException(
            status_code=400,
            detail="Die Google-Verbindung ist abgelaufen — bitte den Vorgang"
            " neu starten (Code einlösen).",
        )
    return pending


def _adopt_pending_google_tokens(source_id: int, pending: Path) -> None:
    tokens = google.load_tokens(pending)
    google.save_tokens(google.token_path(source_id), tokens)
    pending.unlink()


@router.post("/sources", status_code=201)
async def create_source(body: SourceCreate) -> dict:
    """Create a calendar source.

    CalDAV sources must bring a complete, URL-validated config; Google
    sources must reference a pending OAuth flow (``flow_id``) whose parked
    tokens are adopted for the new source. Secrets in the response are
    masked.
    """
    if body.type not in SOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"Unbekannter Quelltyp: {body.type!r}")
    _validate_display_mode(body.display_mode)
    name = _validated_name(body.name)
    config = _filtered_config(body.type, body.config)
    storage = get_storage()
    pending = None
    if body.type == "caldav":
        _validate_caldav_create(config)
    else:
        if not config.get("calendar_id"):
            raise HTTPException(status_code=400, detail="Bitte einen Kalender auswählen.")
        # Check before creating the source so a failed adoption does not
        # leave a token-less source behind.
        pending = _pending_tokens_or_400(body.flow_id)
    source_id = storage.add_source(
        type=body.type,
        name=name,
        config=config,
        display_mode=body.display_mode,
    )
    if pending is not None:
        _adopt_pending_google_tokens(source_id, pending)
    source = storage.get_source(source_id)
    return {"source": _serialize_source(source, 0)}


@router.patch("/sources/{source_id}")
async def update_source(source_id: int, body: SourceUpdate) -> dict:
    """Partially update a source (name, display mode, enabled, config).

    Config updates are whitelisted per type; the secret mask placeholder
    keeps the stored secret, an empty secret is rejected (422), and all
    URLs in the config are re-validated.
    """
    storage = get_storage()
    existing = storage.get_source(source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Quelle nicht gefunden.")
    if body.display_mode is not None:
        _validate_display_mode(body.display_mode)
    name = _validated_name(body.name) if body.name is not None else None
    config = None
    if body.config is not None:
        config = _filtered_config(existing.type, body.config)
        for key in _SECRET_CONFIG_KEYS:
            # The mask placeholder (or an omitted value) means: keep the secret.
            if config.get(key, SECRET_MASK) == SECRET_MASK and existing.config.get(key):
                config[key] = existing.config[key]
            # An empty secret would silently break the next sync — reject it
            # instead of storing it (the stored secret stays untouched).
            if key in config and not config[key]:
                raise HTTPException(
                    status_code=422,
                    detail="Das App-Passwort darf nicht leer sein — zum"
                    " Beibehalten das Feld unverändert lassen.",
                )
        _validate_config_urls(config)
    storage.update_source(
        source_id,
        name=name,
        config=config,
        enabled=body.enabled,
        display_mode=body.display_mode,
    )
    updated = storage.get_source(source_id)
    counts = storage.count_events_by_source()
    return {"source": _serialize_source(updated, counts.get(source_id, 0))}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: int) -> dict:
    """Delete a source, its stored events (FK cascade) and, for Google
    sources, the token file that belonged to it."""
    storage = get_storage()
    source = storage.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Quelle nicht gefunden.")
    storage.delete_source(source_id)
    if source.type == "google":
        tokens_file = google.token_path(source_id)
        if tokens_file.exists():
            tokens_file.unlink()
    return {"deleted": source_id}


# -- CalDAV connection test -------------------------------------------------


@router.post("/caldav/calendars")
async def list_caldav_calendars(probe: CaldavProbe) -> dict:
    """Connection test: list the account's calendars for selection."""
    try:
        validate_source_url(probe.url)
    except SourceURLError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config = {
        "url": probe.url,
        "username": probe.username,
        "app_password": probe.app_password,
    }
    try:
        calendars = await caldav.list_calendars(config)
    except Exception as exc:
        # The exception text may quote the request URL with credentials.
        error = sanitize_error(str(exc))
        logger.warning("CalDAV connection test failed: %s", error)
        raise HTTPException(
            status_code=502,
            detail=f"Verbindung fehlgeschlagen: {error}",
        ) from exc
    return {"calendars": calendars}


# -- Google OAuth flow -------------------------------------------------------


def _google_credentials_or_400() -> tuple[str, str]:
    storage = get_storage()
    client_id = storage.get_setting(settings.GOOGLE_CLIENT_ID_KEY)
    client_secret = storage.get_setting(settings.GOOGLE_CLIENT_SECRET_KEY)
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Bitte zuerst Client-ID und Client-Secret hinterlegen.",
        )
    return client_id, client_secret


@router.post("/google/auth-url")
async def google_auth_url() -> dict:
    """Start of the OAuth flow: build the consent URL for the admin UI."""
    google.cleanup_stale_pending_tokens()
    client_id, _ = _google_credentials_or_400()
    return {"auth_url": google_oauth.build_auth_url(client_id)}


@router.post("/google/connect")
async def google_connect(body: GoogleConnect) -> dict:
    """Exchange the pasted code, park the tokens, return the calendar list.

    The tokens are parked in a per-flow pending file; the returned random
    ``flow_id`` is the claim ticket that ``create_source`` needs to adopt
    them (nobody can adopt tokens they did not just receive the id for).
    """
    google.cleanup_stale_pending_tokens()
    client_id, client_secret = _google_credentials_or_400()
    try:
        code = google_oauth.extract_auth_code(body.code)
        tokens = await google_oauth.exchange_code(
            code, client_id=client_id, client_secret=client_secret
        )
        flow_id = secrets.token_urlsafe(16)
        google.save_tokens(google.pending_token_path(flow_id), tokens)
        calendars = await google_oauth.fetch_calendar_list(tokens["access_token"])
    except google_oauth.GoogleOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Network errors etc. — the exception text may quote request URLs;
        # same sanitizing pattern as the CalDAV connection test.
        error = sanitize_error(str(exc))
        logger.warning("Google connect failed: %s", error)
        raise HTTPException(
            status_code=502,
            detail=f"Google-Verbindung fehlgeschlagen: {error}",
        ) from exc
    return {"flow_id": flow_id, "calendars": calendars}


@router.delete("/google/pending/{flow_id}")
async def delete_google_pending(flow_id: str) -> dict:
    """Abort a pending OAuth flow: discard its parked tokens.

    Idempotent — the wizard reset calls this unconditionally, so an
    already adopted or cleaned-up flow is not an error.
    """
    try:
        pending = google.pending_token_path(flow_id)
    except google.InvalidFlowIdError as exc:
        raise HTTPException(status_code=400, detail="Ungültige Flow-ID.") from exc
    pending.unlink(missing_ok=True)
    return {"deleted": flow_id}
