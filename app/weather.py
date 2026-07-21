"""Weather view API (/api/weather): forecast for Munich from MET Norway.

Data source is MET Norway's free Locationforecast 2.0 product (no API key).
Their terms of service put three obligations on us, all implemented here:

1. **Identify ourselves** — every request carries a descriptive
   ``User-Agent`` including a contact (the repository URL). Requests
   without one are rejected by MET.
2. **Cache and use conditional requests** — the response is cached server
   side for at least 30 minutes (``FORECAST_CACHE_TTL_SECONDS``); the
   ``Expires`` header may extend that, and a refetch sends
   ``If-Modified-Since`` from the previous ``Last-Modified`` so an
   unchanged forecast costs MET a 304 instead of a full body.
3. **Attribute the source** — the frontend shows "Daten: MET Norway / Yr".

The location is a fixed constant (Munich); nothing about the outgoing
request is user-controlled, so there is no SSRF surface. Hardening mirrors
``app.power``: request timeout, hard response size limit, a lock against a
thundering herd on a cache miss, and short-lived caching of errors so a
down service is not hammered by every polling kiosk. Failures surface as
HTTP 502 with a German message the frontend shows verbatim.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import time
from email.utils import parsedate_to_datetime

import httpx
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/weather")

# Munich (city centre). Fixed constant — never user-supplied.
MUNICH_LAT = 48.1374
MUNICH_LON = 11.5755

MET_BASE_URL = "https://api.met.no/weatherapi/locationforecast/2.0"
MET_FORECAST_PATH = "compact"
# MET Norway requires a descriptive User-Agent with a way to contact us.
USER_AGENT = "Familienkalender/1.0 github.com/roland1910/Familienkalender"

REQUEST_TIMEOUT_SECONDS = 15.0
# MET asks clients not to poll more often than the data changes; the model
# runs hourly at best, so 30 minutes is the floor we promise them.
FORECAST_CACHE_TTL_SECONDS = 1800.0
# Upper bound even when Expires points much further out — the kiosk should
# still pick up a new forecast within a few hours.
MAX_FORECAST_CACHE_TTL_SECONDS = 6 * 3600.0
# Short TTL for cached *errors* (same rationale as app.power): a down
# service must not be re-hit by every poll, but a recovery shows up soon.
ERROR_CACHE_TTL_SECONDS = 60.0
# The compact product for 48h is a few tens of KB; this is a generous cap
# that still stops a runaway response from being buffered in full.
MAX_FORECAST_RESPONSE_BYTES = 4 * 1024 * 1024

# How far ahead the forecast is delivered. The frontend switches between
# 24h and 48h purely client-side, so one cached payload serves both.
FORECAST_HOURS = 48
# Entries older than this (relative to now) are dropped; one hour of slack
# keeps the *current* hour's entry when we are in the middle of it.
FORECAST_PAST_SLACK_SECONDS = 3600

# MET answers 203 for a deprecated product version — still a usable body.
_OK_STATUSES = frozenset({200, 203})


class WeatherUnavailableError(Exception):
    """An upstream weather service cannot deliver right now.

    The message is German and shown verbatim in the frontend error state.
    """


def _now() -> float:
    """Monotonic clock; wrapped so tests can control cache expiry."""
    return time.monotonic()


# -- forecast cache ---------------------------------------------------------

_forecast_payload: dict | None = None
_forecast_valid_until = 0.0
# Last-Modified of the cached payload, replayed as If-Modified-Since.
_forecast_last_modified: str | None = None
_forecast_error: str | None = None
_forecast_error_valid_until = 0.0
_forecast_lock = asyncio.Lock()


def reset_caches() -> None:
    """Drop every cached weather payload and error (tests)."""
    global _forecast_payload, _forecast_valid_until, _forecast_last_modified
    global _forecast_error, _forecast_error_valid_until
    _forecast_payload = None
    _forecast_valid_until = 0.0
    _forecast_last_modified = None
    _forecast_error = None
    _forecast_error_valid_until = 0.0


def create_met_client() -> httpx.AsyncClient:
    """HTTP client for MET Norway (fixed host, descriptive User-Agent)."""
    return httpx.AsyncClient(
        base_url=MET_BASE_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _number(value: object) -> float | None:
    """A finite float from a JSON value, else None (missing/non-numeric).

    ``bool`` is excluded on purpose: it is an ``int`` subclass, and a
    stray ``true`` should read as "no value", not as 1 °C.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _details(block: object) -> dict:
    """The ``details`` mapping of a MET data block, or an empty dict."""
    if not isinstance(block, dict):
        return {}
    details = block.get("details")
    return details if isinstance(details, dict) else {}


def parse_forecast(data: object, *, now: dt.datetime, hours: int = FORECAST_HOURS) -> list[dict]:
    """MET's timeseries → ``[{t, temp_c, precip_mm, wind_ms, wind_dir_deg}, ...]``.

    ``t`` is epoch milliseconds (UTC). Individual missing or non-numeric
    values become ``None`` rather than dropping the whole hour — the chart
    simply skips them. Entries without a parseable ``time`` are skipped;
    a completely unusable body raises ``WeatherUnavailableError``.
    """
    if not isinstance(data, dict):
        raise WeatherUnavailableError("Der Wetterdienst liefert eine unerwartete Antwort.")
    properties = data.get("properties")
    series = properties.get("timeseries") if isinstance(properties, dict) else None
    if not isinstance(series, list):
        raise WeatherUnavailableError("Der Wetterdienst liefert eine unerwartete Antwort.")

    earliest = now - dt.timedelta(seconds=FORECAST_PAST_SLACK_SECONDS)
    latest = now + dt.timedelta(hours=hours)
    points: list[dict] = []
    for entry in series:
        if not isinstance(entry, dict):
            continue
        moment = _parse_moment(entry.get("time"))
        if moment is None or moment < earliest or moment > latest:
            continue
        block = entry.get("data")
        instant = _details(block.get("instant") if isinstance(block, dict) else None)
        next_hour = _details(block.get("next_1_hours") if isinstance(block, dict) else None)
        points.append(
            {
                "t": int(moment.timestamp() * 1000),
                "temp_c": _number(instant.get("air_temperature")),
                "precip_mm": _number(next_hour.get("precipitation_amount")),
                "wind_ms": _number(instant.get("wind_speed")),
                "wind_dir_deg": _number(instant.get("wind_from_direction")),
            }
        )
    points.sort(key=lambda point: point["t"])
    return points


def _parse_moment(raw: object) -> dt.datetime | None:
    """An aware UTC datetime from MET's ISO-8601 ``time``, else None."""
    if not isinstance(raw, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


async def _read_limited(response: httpx.Response, limit: int, message: str) -> bytes:
    """Read a streamed body under a hard size cap.

    A missing or lying Content-Length must not bypass the cap, so the
    streamed chunks are counted as well.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise WeatherUnavailableError(message)
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise WeatherUnavailableError(message)
        chunks.append(chunk)
    return b"".join(chunks)


def _cache_ttl_from_expires(headers: httpx.Headers) -> float:
    """Cache lifetime honouring MET's ``Expires``, clamped to our own bounds.

    Never shorter than FORECAST_CACHE_TTL_SECONDS (our politeness floor)
    and never longer than MAX_FORECAST_CACHE_TTL_SECONDS.
    """
    raw = headers.get("Expires")
    if not raw:
        return FORECAST_CACHE_TTL_SECONDS
    try:
        expires = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return FORECAST_CACHE_TTL_SECONDS
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    seconds = (expires - dt.datetime.now(dt.UTC)).total_seconds()
    return min(max(seconds, FORECAST_CACHE_TTL_SECONDS), MAX_FORECAST_CACHE_TTL_SECONDS)


async def _fetch_forecast_uncached() -> tuple[dict | None, str | None, float]:
    """Fetch the forecast from MET.

    Returns ``(payload, last_modified, ttl)``; ``payload`` is ``None`` for a
    304 Not Modified, meaning "keep what is cached, just extend its TTL".
    """
    params = {"lat": str(MUNICH_LAT), "lon": str(MUNICH_LON)}
    headers = {}
    if _forecast_payload is not None and _forecast_last_modified:
        headers["If-Modified-Since"] = _forecast_last_modified
    async with create_met_client() as client:
        try:
            request = client.build_request(
                "GET", MET_FORECAST_PATH, params=params, headers=headers
            )
            response = await client.send(request, stream=True)
            try:
                if response.status_code == 304:
                    return None, _forecast_last_modified, _cache_ttl_from_expires(response.headers)
                if response.status_code not in _OK_STATUSES:
                    raise WeatherUnavailableError(
                        f"Der Wetterdienst antwortet mit Fehler (HTTP {response.status_code})."
                    )
                body = await _read_limited(
                    response,
                    MAX_FORECAST_RESPONSE_BYTES,
                    "Der Wetterdienst antwortet mit einer zu großen Antwort.",
                )
                last_modified = response.headers.get("Last-Modified")
                ttl = _cache_ttl_from_expires(response.headers)
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise WeatherUnavailableError("Der Wetterdienst ist nicht erreichbar.") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WeatherUnavailableError(
            "Der Wetterdienst liefert eine unlesbare Antwort."
        ) from exc
    points = parse_forecast(data, now=dt.datetime.now(dt.UTC))
    return {"points": points}, last_modified, ttl


async def _fetch_forecast() -> dict:
    """Cache-and-lock-aware forecast fetch (see the module docstring)."""
    global _forecast_payload, _forecast_valid_until, _forecast_last_modified
    global _forecast_error, _forecast_error_valid_until
    cached = _cached_forecast()
    if cached is not None:
        return _payload_or_raise(cached)
    async with _forecast_lock:
        # Another caller may have filled the cache while we waited.
        cached = _cached_forecast()
        if cached is not None:
            return _payload_or_raise(cached)
        try:
            payload, last_modified, ttl = await _fetch_forecast_uncached()
        except WeatherUnavailableError as exc:
            _forecast_error = str(exc)
            _forecast_error_valid_until = _now() + ERROR_CACHE_TTL_SECONDS
            raise
        if payload is not None:
            _forecast_payload = payload
            _forecast_last_modified = last_modified
        _forecast_valid_until = _now() + ttl
        _forecast_error = None
        _forecast_error_valid_until = 0.0
        if _forecast_payload is None:  # 304 without anything cached
            raise WeatherUnavailableError("Der Wetterdienst liefert keine Vorhersage.")
        return _forecast_payload


def _cached_forecast() -> dict | str | None:
    """The still-valid cached payload or error message, if any."""
    now = _now()
    if _forecast_payload is not None and now < _forecast_valid_until:
        return _forecast_payload
    if _forecast_error is not None and now < _forecast_error_valid_until:
        return _forecast_error
    return None


def _payload_or_raise(cached: dict | str) -> dict:
    """Return a cached payload, or re-raise a cached error."""
    if isinstance(cached, str):
        raise WeatherUnavailableError(cached)
    return cached


@router.get("/forecast")
async def get_forecast() -> dict:
    """Hourly forecast for Munich, ~48 hours ahead (served from cache)."""
    try:
        return await _fetch_forecast()
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
