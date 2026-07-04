"""Typed access to persisted admin settings.

Settings live in the SQLite ``settings`` table (key/value, see
app.storage). This module owns the known keys and the parsing/fallback
logic so API and sync code never deal with raw strings.
"""

import json
import os
from dataclasses import dataclass
from datetime import time

from app.filtering import DEFAULT_EVENING_BOUNDARY
from app.storage import Storage

EVENING_BOUNDARY_KEY = "evening_boundary"
# Google OAuth app credentials (Desktop client). The client secret is a
# secret: it must never be returned by any GET API (see app.admin).
GOOGLE_CLIENT_ID_KEY = "google_client_id"
GOOGLE_CLIENT_SECRET_KEY = "google_client_secret"
# Smart-plug sensors shown as the device list in the power view, stored as
# a JSON array of {"entity_id", "name"} objects.
POWER_DEVICES_KEY = "power_devices"


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
    """
    raw = storage.get_setting(POWER_DEVICES_KEY)
    if raw is None:
        return list(DEFAULT_POWER_DEVICES)
    try:
        items = json.loads(raw)
        return [PowerDevice(item["entity_id"], item["name"]) for item in items]
    except (ValueError, TypeError, KeyError):
        return list(DEFAULT_POWER_DEVICES)


def set_power_devices(storage: Storage, devices: list[PowerDevice]) -> None:
    """Persist the power-view device list (validation happens in the API layer)."""
    storage.set_setting(
        POWER_DEVICES_KEY,
        json.dumps(
            [{"entity_id": device.entity_id, "name": device.name} for device in devices]
        ),
    )
