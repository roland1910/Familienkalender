"""Admin API (/api/admin/*): sources CRUD, settings, OAuth connect.

Reachability: like every other route, these endpoints sit behind HA
ingress plus the client-IP allowlist middleware. On top of that the
whole router requires an HA admin user (require_admin dependency —
resolved from the ingress user headers, see app.auth); non-admins get
403 "Nur für Administratoren.".

Secret handling: app passwords and the Google client secret are stored
(source config JSON / settings table) but never returned by any endpoint.
Configs in responses carry a mask placeholder instead of the password;
a PATCH sending the placeholder back keeps the stored secret.
"""

import logging
import secrets
from datetime import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import feed_constants, google_oauth, power, settings
from app.auth import require_admin
from app.models import (
    DISPLAY_MODES,
    FEED_PRIORITY_MAX,
    FEED_PRIORITY_MIN,
    SHORTCODE_MAX_LENGTH,
    SOURCE_TYPES,
    Source,
    is_valid_feed_priority,
    is_valid_shortcode,
    is_valid_source_color,
)
from app.sanitize import sanitize_error
from app.settings import get_evening_boundary
from app.sources import caldav, google
from app.storage import get_storage
from app.url_validation import SourceURLError, validate_source_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

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
    # A contacts (birthdays) source carries no config — the connected
    # account defines whose contacts are read.
    "google_contacts": (),
}

# Source types that authenticate via the copy-paste Google OAuth flow and
# store their tokens in google_token_<source_id>.json.
_GOOGLE_TOKEN_TYPES = ("google", "google_contacts")

# Source names are display strings; the cap keeps hostile input from
# bloating the DB and the admin UI.
MAX_NAME_LENGTH = 200

MAX_POWER_DEVICES = 30
MAX_POWER_DEVICE_NAME_LENGTH = 100

# Host port for the public subscription URL — shared constant, see
# app/feed_constants.py (re-exported here so existing imports keep working).
FEED_HOST_PORT = feed_constants.FEED_HOST_PORT


class SettingsUpdate(BaseModel):
    evening_boundary: str


class PowerDeviceIn(BaseModel):
    entity_id: str
    # Optional display-name override; empty means "use the HA friendly_name".
    name: str = ""


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
    # Title prefix for the ICS feed; "" clears it, None leaves it untouched.
    shortcode: str | None = None
    # Display color "#rrggbb"; "" resets to the default palette, None
    # leaves it untouched.
    color: str | None = None
    # Whether the source's family-relevant events appear in the ICS feed.
    include_in_feed: bool | None = None
    # Precedence when the feed collapses duplicate events across sources
    # (higher wins). None leaves it untouched.
    feed_priority: int | None = None


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


def _validated_shortcode(raw: str) -> str:
    """Normalized (trimmed, uppercased) shortcode, or 400 for invalid input."""
    shortcode = raw.strip().upper()
    if not is_valid_shortcode(shortcode):
        raise HTTPException(
            status_code=400,
            detail="Ungültiges Kürzel — höchstens"
            f" {SHORTCODE_MAX_LENGTH} Zeichen aus A-Z und 0-9.",
        )
    return shortcode


def _validated_color(raw: str) -> str:
    """Normalized (trimmed, lowercased) color, or 400 for invalid input.

    Strict "#rrggbb" or empty only — the value is interpolated into a CSS
    custom property in the frontend, so nothing else may pass.
    """
    color = raw.strip().lower()
    if not is_valid_source_color(color):
        raise HTTPException(
            status_code=400,
            detail="Ungültige Farbe — bitte im Format #rrggbb angeben"
            " (oder leer lassen für die Standardfarbe).",
        )
    return color


def _validated_feed_priority(raw: int) -> int:
    """The feed priority if in range, else 400 with a German message."""
    if not is_valid_feed_priority(raw):
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Vorrang — bitte eine ganze Zahl zwischen"
            f" {FEED_PRIORITY_MIN} und {FEED_PRIORITY_MAX} angeben.",
        )
    return raw


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
        "shortcode": source.shortcode,
        "color": source.color,
        "include_in_feed": source.include_in_feed,
        "feed_priority": source.feed_priority,
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
    """Replace the power-view device list (an empty list is valid).

    Rejects (400, German message, with the 1-based line/position where it
    helps) anything that would not make sense to send to HA as a sensor
    entity id, plus two list-level conflicts: an id already used by one of
    the fixed aggregate sensors (app.power.AGGREGATE_ENTITIES), and an id
    repeated within the submitted list.
    """
    devices = []
    seen_entity_ids: set[str] = set()
    for index, item in enumerate(update.devices, start=1):
        entity_id = item.entity_id.strip()
        name = item.name.strip()
        if not settings.is_valid_power_entity_id(entity_id):
            raise HTTPException(
                status_code=400,
                detail=f"Zeile {index}: Ungültige Entity-ID: {item.entity_id!r}",
            )
        if entity_id in power.AGGREGATE_ENTITIES.values():
            raise HTTPException(
                status_code=400,
                detail=f"Zeile {index}: Die Entity-ID {entity_id!r} wird bereits"
                " für eine feste Kennzahl der Strom-Ansicht verwendet.",
            )
        if entity_id in seen_entity_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Zeile {index}: Die Entity-ID {entity_id!r} ist doppelt"
                " in der Liste.",
            )
        seen_entity_ids.add(entity_id)
        # An empty name is allowed: the power view then shows the sensor's
        # HA friendly_name (with the entity_id as a last-resort fallback).
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


# -- feed subscription -------------------------------------------------------


def _request_hostname(request: Request) -> str | None:
    """Best-effort hostname of the HA instance as the browser sees it.

    Behind ingress the browser talks to the HA frontend, so its host
    (X-Forwarded-Host if the proxy set it, else Host) is the fallback
    guess for where the feed port is reachable — used only while no
    public host is configured (settings.get_feed_public_host).
    """
    raw = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not raw:
        return None
    raw = raw.split(",")[0].strip()
    if raw.startswith("["):
        # IPv6 literal: keep the brackets, drop a trailing :port.
        return raw.split("]")[0] + "]"
    return raw.split(":")[0] or None


def _feed_payload(request: Request, token: str) -> dict:
    path = f"/feed/{token}.ics"
    public_host = settings.get_feed_public_host(get_storage())
    hostname = public_host or _request_hostname(request)
    return {
        "feed": {
            "path": path,
            # The feed listener terminates TLS itself (app.serve), so the
            # subscription URL is https on the forwarded port.
            "url": f"https://{hostname}:{FEED_HOST_PORT}{path}" if hostname else None,
            "public_host": public_host,
        }
    }


class FeedHostUpdate(BaseModel):
    host: str


@router.get("/feed")
async def get_feed(request: Request) -> dict:
    """Subscription URL for the ICS feed; generates the token on first call."""
    return _feed_payload(request, settings.ensure_feed_token(get_storage()))


@router.post("/feed/rotate")
async def rotate_feed(request: Request) -> dict:
    """Replace the feed token — every previously shared feed URL stops working."""
    return _feed_payload(request, settings.rotate_feed_token(get_storage()))


@router.put("/feed/host")
async def update_feed_host(update: FeedHostUpdate, request: Request) -> dict:
    """Set the public hostname for the displayed subscription URL.

    An empty value clears the override (back to the request-host guess).
    Bare hostname/IPv4 only — the value goes verbatim into the URL.
    """
    host = update.host.strip()
    if host and not settings.is_valid_public_host(host):
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Host — bitte nur den Hostnamen angeben"
            " (ohne https://, Port oder Pfad), z. B. rnd.ignorelist.com.",
        )
    storage = get_storage()
    settings.set_feed_public_host(storage, host)
    return _feed_payload(request, settings.ensure_feed_token(storage))


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
    # Birthdays (google_contacts) are all-day events and thus always family
    # relevant — the display mode has no effect, so it is fixed to "full"
    # and the request value is ignored (the wizard offers no choice).
    display_mode = "full" if body.type == "google_contacts" else body.display_mode
    _validate_display_mode(display_mode)
    name = _validated_name(body.name)
    config = _filtered_config(body.type, body.config)
    storage = get_storage()
    pending = None
    if body.type == "caldav":
        _validate_caldav_create(config)
    elif body.type == "google":
        if not config.get("calendar_id"):
            raise HTTPException(status_code=400, detail="Bitte einen Kalender auswählen.")
        # Check before creating the source so a failed adoption does not
        # leave a token-less source behind.
        pending = _pending_tokens_or_400(body.flow_id)
    else:  # google_contacts — no calendar to pick, just adopt the tokens.
        pending = _pending_tokens_or_400(body.flow_id)
    source_id = storage.add_source(
        type=body.type,
        name=name,
        config=config,
        display_mode=display_mode,
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
    display_mode = body.display_mode
    # google_contacts is fixed to "full" (all-day birthdays are always
    # family relevant): silently ignore any display_mode change for it,
    # while all other fields keep updating normally.
    if existing.type == "google_contacts":
        display_mode = None
    if display_mode is not None:
        _validate_display_mode(display_mode)
    name = _validated_name(body.name) if body.name is not None else None
    shortcode = _validated_shortcode(body.shortcode) if body.shortcode is not None else None
    color = _validated_color(body.color) if body.color is not None else None
    feed_priority = (
        _validated_feed_priority(body.feed_priority)
        if body.feed_priority is not None
        else None
    )
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
        display_mode=display_mode,
        shortcode=shortcode,
        color=color,
        include_in_feed=body.include_in_feed,
        feed_priority=feed_priority,
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
    if source.type in _GOOGLE_TOKEN_TYPES:
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


@router.post("/google/contacts-auth-url")
async def google_contacts_auth_url() -> dict:
    """Start of the birthdays OAuth flow: consent URL with the contacts scope.

    Unlike the calendar flow this requests contacts.readonly (People API);
    the Google "Birthdays" calendar is not reachable via the Calendar API.
    """
    google.cleanup_stale_pending_tokens()
    client_id, _ = _google_credentials_or_400()
    return {
        "auth_url": google_oauth.build_auth_url(
            client_id, scope=google_oauth.CONTACTS_SCOPE
        )
    }


@router.post("/google/contacts-connect")
async def google_contacts_connect(body: GoogleConnect) -> dict:
    """Exchange the pasted code and park the tokens for a birthdays source.

    Mirrors /google/connect but does NOT fetch a calendar list (the People
    API has no calendars): the source is created directly from the parked
    tokens. Returns only the random ``flow_id`` claim ticket.
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
    except google_oauth.GoogleOAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        error = sanitize_error(str(exc))
        logger.warning("Google contacts connect failed: %s", error)
        raise HTTPException(
            status_code=502,
            detail=f"Google-Verbindung fehlgeschlagen: {error}",
        ) from exc
    return {"flow_id": flow_id}


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
