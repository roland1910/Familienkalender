"""Power view API (/api/power): live PV production and consumption.

Data source is the Home Assistant Core API of the host instance, read
via the standard mechanism for add-ons with ``homeassistant_api``:
``http://supervisor/core/api`` with the SUPERVISOR_TOKEN. For local
development and tests both are overridable via HA_API_URL / HA_API_TOKEN.

The aggregate sensors (production, total consumption, balance, surplus,
grid import) are fixed template sensors of the household's HA instance;
only the smart-plug device list is configurable (settings table, managed
in the admin UI — see app.settings).

Responses are cached server side for a few seconds so polling frontends
(kiosk display plus ingress panels) do not hammer HA. Sensors reporting
``unavailable``/``unknown`` (or a non-numeric state) are returned as 0
with ``available: false``; HA being unreachable or a sensor missing
entirely is an error (HTTP 502, German message) so the view can show a
proper error state.
"""

import asyncio
import os
import time

import httpx
from fastapi import APIRouter, HTTPException

from app.settings import get_power_devices
from app.storage import get_storage

router = APIRouter(prefix="/api/power")

DEFAULT_HA_API_URL = "http://supervisor/core/api"
REQUEST_TIMEOUT_SECONDS = 5.0
CACHE_TTL_SECONDS = 10.0
# Short TTL for cached *errors*: long enough that a down HA instance is not
# hammered by every poller (kiosk display plus ingress panels, each polling
# every 15s), short enough that a recovered HA is picked up quickly.
ERROR_CACHE_TTL_SECONDS = 5.0

# Fixed aggregate sensors (template sensors + inverter) → payload keys.
AGGREGATE_ENTITIES = {
    "production": "sensor.hoymiles_station_balkonkraftwerk_current_power",
    "consumption": "sensor.stromverbrauch_gesamt",
    "balance": "sensor.strom_bilanz",
    "surplus": "sensor.strom_ueberschuss",
    "grid_import": "sensor.strom_netzbezug",
}

# States HA uses for sensors that exist but currently have no value.
_NO_VALUE_STATES = frozenset({"unavailable", "unknown", "none", ""})


class HomeAssistantUnavailableError(Exception):
    """The HA Core API cannot deliver the requested states right now.

    The message is German and shown verbatim in the frontend error state.
    """


# Server-side response cache: one payload for all clients.
_cached_payload: dict | None = None
_cache_valid_until = 0.0

# Cached error message, separate from the payload cache so a failing HA
# instance is not refetched on every poll from every client. All errors are
# reported as 502, so only the message needs caching.
_cached_error: str | None = None
_error_cache_valid_until = 0.0

# Guards _fetch_snapshot so a cache miss triggers exactly one fetch; requests
# that arrive while a fetch is in flight wait for it instead of each
# starting their own (thundering-herd protection when HA is slow or down).
_fetch_lock = asyncio.Lock()


def _now() -> float:
    """Monotonic clock; wrapped so tests can control cache expiry."""
    return time.monotonic()


def reset_cache() -> None:
    """Drop the cached payload and error (tests; device-list changes in the admin API)."""
    global _cached_payload, _cache_valid_until, _cached_error, _error_cache_valid_until
    _cached_payload = None
    _cache_valid_until = 0.0
    _cached_error = None
    _error_cache_valid_until = 0.0


def create_client() -> httpx.AsyncClient:
    """HTTP client for the HA Core API (env overrides for local dev/tests)."""
    base_url = os.environ.get("HA_API_URL") or DEFAULT_HA_API_URL
    token = os.environ.get("HA_API_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _parse_state(state: str) -> tuple[float, bool]:
    """(value in W, available) for a raw HA state string.

    ``unavailable``/``unknown`` and non-numeric states become 0 with
    ``available=False`` — the sensor exists, it just has no value right now.
    """
    if state.lower() in _NO_VALUE_STATES:
        return 0.0, False
    try:
        return float(state), True
    except ValueError:
        return 0.0, False


async def _fetch_metric(client: httpx.AsyncClient, entity_id: str) -> dict:
    """One sensor as ``{"value", "available"}``; errors become German messages."""
    try:
        response = await client.get(f"states/{entity_id}")
    except httpx.HTTPError as exc:
        raise HomeAssistantUnavailableError(
            "Home Assistant ist nicht erreichbar."
        ) from exc
    if response.status_code == 404:
        raise HomeAssistantUnavailableError(
            f"Sensor {entity_id} ist in Home Assistant unbekannt."
        )
    if response.status_code != 200:
        raise HomeAssistantUnavailableError(
            f"Home Assistant antwortet mit Fehler (HTTP {response.status_code})."
        )
    value, available = _parse_state(str(response.json().get("state", "")))
    return {"value": value, "available": available}


async def _fetch_snapshot_uncached() -> dict:
    """Fetch all sensors concurrently and build the /api/power payload."""
    devices = get_power_devices(get_storage())
    entity_ids = [*AGGREGATE_ENTITIES.values(), *(device.entity_id for device in devices)]
    async with create_client() as client:
        results = await asyncio.gather(
            *(_fetch_metric(client, entity_id) for entity_id in entity_ids),
            # Let every request finish before the client closes, then surface
            # the first error — gather would otherwise leave requests running.
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    metrics = dict(zip(entity_ids, results, strict=True))
    payload: dict = {
        key: metrics[entity_id] for key, entity_id in AGGREGATE_ENTITIES.items()
    }
    payload["devices"] = [
        {"entity_id": device.entity_id, "name": device.name, **metrics[device.entity_id]}
        for device in devices
    ]
    return payload


def _cached_response() -> dict | tuple[str, int] | None:
    """The still-valid cached payload or error, if any."""
    now = _now()
    if _cached_payload is not None and now < _cache_valid_until:
        return _cached_payload
    if _cached_error is not None and now < _error_cache_valid_until:
        return _cached_error
    return None


async def _fetch_snapshot() -> dict:
    """Cache-and-lock-aware snapshot: serves the cache, else fetches once.

    On a cache miss (payload or error) this takes ``_fetch_lock`` so
    concurrent callers (multiple pollers hitting a miss at the same moment)
    share a single HA fetch instead of each firing their own — both for the
    happy path and for a failing HA instance, whose error is cached for a
    short TTL as well (protects against a thundering herd when HA is slow
    or down). Raises ``HomeAssistantUnavailableError`` for both a fresh and
    a cached error, so callers only need to handle one exception type.
    """
    global _cached_payload, _cache_valid_until, _cached_error, _error_cache_valid_until
    cached = _cached_response()
    if cached is not None:
        return _payload_or_raise(cached)
    async with _fetch_lock:
        # Re-check: another caller may have populated the cache while this
        # one was waiting for the lock.
        cached = _cached_response()
        if cached is not None:
            return _payload_or_raise(cached)
        try:
            payload = await _fetch_snapshot_uncached()
        except HomeAssistantUnavailableError as exc:
            _cached_error = str(exc)
            _error_cache_valid_until = _now() + ERROR_CACHE_TTL_SECONDS
            raise
        _cached_payload = payload
        _cache_valid_until = _now() + CACHE_TTL_SECONDS
        return payload


def _payload_or_raise(cached: dict | str) -> dict:
    """Return a cached payload, or re-raise a cached error."""
    if isinstance(cached, str):
        raise HomeAssistantUnavailableError(cached)
    return cached


@router.get("")
async def get_power() -> dict:
    """Current power values for the view; served from cache within the TTL."""
    try:
        return await _fetch_snapshot()
    except HomeAssistantUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
