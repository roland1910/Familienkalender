"""Typed access to persisted admin settings.

Settings live in the SQLite ``settings`` table (key/value, see
app.storage). This module owns the known keys and the parsing/fallback
logic so API and sync code never deal with raw strings.
"""

import json
import logging
import os
import re
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
    """One device row of the power view: an HA sensor plus a display name."""

    entity_id: str
    name: str


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
        devices = [PowerDevice(item["entity_id"], item["name"]) for item in items]
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
