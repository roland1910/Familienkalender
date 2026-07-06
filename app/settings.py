"""Typed access to persisted admin settings.

Settings live in the SQLite ``settings`` table (key/value, see
app.storage). This module owns the known keys and the parsing/fallback
logic so API and sync code never deal with raw strings.
"""

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import time

from app.filtering import DEFAULT_EVENING_BOUNDARY
from app.storage import Storage

logger = logging.getLogger(__name__)

EVENING_BOUNDARY_KEY = "evening_boundary"
# Google OAuth app credentials (Desktop client). The client secret is a
# secret: it must never be returned by any GET API (see app.admin).
GOOGLE_CLIENT_ID_KEY = "google_client_id"
GOOGLE_CLIENT_SECRET_KEY = "google_client_secret"
# Smart-plug sensors shown as the device list in the power view, stored as
# a JSON array of {"entity_id", "name"} objects.
POWER_DEVICES_KEY = "power_devices"
# URL token protecting the subscribable ICS feed (GET /feed/<token>.ics).
# It is the sole auth on the dedicated feed port, so it is generated with
# plenty of entropy and never returned by any non-admin endpoint.
FEED_TOKEN_KEY = "feed_token"
# Public hostname shown in the admin UI's subscription URL (the router
# forwards external port 8098 to the feed listener). Bare host only —
# no scheme, port or path; empty/missing falls back to the request host.
FEED_PUBLIC_HOST_KEY = "feed_public_host"

# DNS limits: 253 chars total, labels of 1-63 chars, letters/digits/
# hyphens, no leading/trailing hyphen. Also matches plain IPv4 literals.
MAX_PUBLIC_HOST_LENGTH = 253
_HOST_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
PUBLIC_HOST_PATTERN = re.compile(rf"^{_HOST_LABEL}(\.{_HOST_LABEL})*$", re.IGNORECASE)

# HA entity ids are lowercase domain.object_id. Shared between the admin API
# (validates on write) and get_power_devices (defense in depth on read, in
# case the stored value was ever written by a future/other code path).
POWER_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# HA entity ids in practice are far shorter; this is a defensive upper bound
# against pathological input, not a realistic sensor name length.
MAX_POWER_ENTITY_ID_LENGTH = 255


def is_valid_power_entity_id(entity_id: str) -> bool:
    """Whether entity_id is a plausible HA sensor entity id for the power view."""
    return (
        len(entity_id) <= MAX_POWER_ENTITY_ID_LENGTH
        and POWER_ENTITY_ID_PATTERN.fullmatch(entity_id) is not None
    )


@dataclass(frozen=True)
class PowerDevice:
    """One device row of the power view: an HA sensor plus a display name.

    ``name`` is an optional override. When empty, the power view uses the
    sensor's HA ``friendly_name`` instead (see app.power / power-view.js).
    """

    entity_id: str
    name: str = ""


# The household's smart plugs with German display names — used until the
# list is edited in the admin UI.
DEFAULT_POWER_DEVICES = (
    PowerDevice("sensor.kuhlschrank_leistung", "Kühlschrank"),
    PowerDevice("sensor.tv_sideboard_leistung", "TV-Sideboard"),
    PowerDevice("sensor.spuhlmaschiene_leistung", "Spülmaschine"),
    PowerDevice("sensor.schreibtisch_leistung", "Schreibtisch"),
    PowerDevice("sensor.matter_over_wifi_smart_plug_6_leistung", "Steckdose 6"),
)


def get_evening_boundary(storage: Storage) -> time:
    """Evening boundary for the family filter (HH:MM).

    Resolution order: persisted admin setting → EVENING_BOUNDARY env var
    (kept as a fallback for tests and local development without a DB) →
    default 17:00. Invalid values fall through to the next stage.
    """
    candidates = (storage.get_setting(EVENING_BOUNDARY_KEY), os.environ.get("EVENING_BOUNDARY"))
    for raw in candidates:
        if raw:
            try:
                return time.fromisoformat(raw)
            except ValueError:
                continue
    return DEFAULT_EVENING_BOUNDARY


def get_feed_token(storage: Storage) -> str | None:
    """The current feed token, or None while none has been generated yet."""
    return storage.get_setting(FEED_TOKEN_KEY)


def ensure_feed_token(storage: Storage) -> str:
    """The current feed token, generating (and persisting) one if missing."""
    token = storage.get_setting(FEED_TOKEN_KEY)
    if token:
        return token
    return rotate_feed_token(storage)


def rotate_feed_token(storage: Storage) -> str:
    """Replace the feed token with a fresh one — old feed URLs stop working."""
    token = secrets.token_urlsafe(32)
    storage.set_setting(FEED_TOKEN_KEY, token)
    return token


def is_valid_public_host(host: str) -> bool:
    """Whether host is a bare hostname/IPv4 usable in the feed URL.

    Deliberately ASCII-only (internationalized names go in as punycode) —
    the value ends up verbatim in a URL shown by the admin UI.
    """
    return (
        0 < len(host) <= MAX_PUBLIC_HOST_LENGTH
        and PUBLIC_HOST_PATTERN.fullmatch(host) is not None
    )


def get_feed_public_host(storage: Storage) -> str | None:
    """The configured public feed host, or None to use the request host.

    Re-validated on read (defense in depth): a value smuggled into the
    settings table by another write path must not leak into generated URLs.
    """
    raw = storage.get_setting(FEED_PUBLIC_HOST_KEY)
    if raw and is_valid_public_host(raw):
        return raw
    if raw:
        logger.warning("Ignoring invalid stored feed public host: %r", raw)
    return None


def set_feed_public_host(storage: Storage, host: str) -> None:
    """Persist the public feed host; an empty value clears the override
    (validation happens in the API layer)."""
    storage.set_setting(FEED_PUBLIC_HOST_KEY, host)


def get_power_devices(storage: Storage) -> list[PowerDevice]:
    """Device list for the power view; falls back to the defaults.

    An empty stored list is a deliberate choice ("no devices") and is
    returned as such — only a missing or unparseable value falls back.

    Entity ids are re-validated here even though the admin API already
    validates on write (defense in depth: the settings table is trusted
    input today, but a future write path or a manually edited DB should
    not be able to smuggle something odd into a request against HA).
    Individual entries failing the check are skipped and logged rather
    than failing the whole list.
    """
    raw = storage.get_setting(POWER_DEVICES_KEY)
    if raw is None:
        return list(DEFAULT_POWER_DEVICES)
    try:
        items = json.loads(raw)
        # name is optional (empty → use the HA friendly_name at display time).
        devices = [PowerDevice(item["entity_id"], item.get("name") or "") for item in items]
    except (ValueError, TypeError, KeyError):
        return list(DEFAULT_POWER_DEVICES)
    valid_devices = []
    for device in devices:
        if is_valid_power_entity_id(device.entity_id):
            valid_devices.append(device)
        else:
            logger.warning(
                "Skipping power device with invalid entity_id: %r", device.entity_id
            )
    return valid_devices


def set_power_devices(storage: Storage, devices: list[PowerDevice]) -> None:
    """Persist the power-view device list (validation happens in the API layer)."""
    storage.set_setting(
        POWER_DEVICES_KEY,
        json.dumps(
            [{"entity_id": device.entity_id, "name": device.name} for device in devices]
        ),
    )
