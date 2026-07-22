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
import re
import time
from email.utils import parsedate_to_datetime

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

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
# 24h, 48h and 96h purely client-side (Etappe 37), so one cached payload
# serves them all — we hand through MET's full timeseries (it runs ~9 days:
# hourly for ~63h, then 6-hourly). 240h is a ceiling above the whole series,
# so parse_forecast keeps everything MET returns rather than truncating it.
FORECAST_HOURS = 240
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
    _reset_radar_cache()
    _tile_cache.clear()


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
    """MET's timeseries → ``[{t, temp_c, precip_mm, precip_hours, wind_ms,
    wind_dir_deg}, ...]``.

    ``t`` is epoch milliseconds (UTC). Individual missing or non-numeric
    values become ``None`` rather than dropping the whole hour — the chart
    simply skips them. Entries without a parseable ``time`` are skipped;
    a completely unusable body raises ``WeatherUnavailableError``.

    Precipitation comes from ``next_1_hours`` while MET provides it (~63 h),
    and falls back to the ``next_6_hours`` block after that — otherwise the
    96 h chart would show no rain at all in its back third. ``precip_hours``
    says which period the amount covers (1 or 6), so the chart can draw the
    bar across exactly that span.
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
        precip = _number(next_hour.get("precipitation_amount"))
        precip_hours = 1
        if precip is None:
            next_six = _details(block.get("next_6_hours") if isinstance(block, dict) else None)
            precip = _number(next_six.get("precipitation_amount"))
            if precip is not None:
                precip_hours = 6
        points.append(
            {
                "t": int(moment.timestamp() * 1000),
                "temp_c": _number(instant.get("air_temperature")),
                "precip_mm": precip,
                "precip_hours": precip_hours,
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
    """Forecast for Munich, MET's full timeseries (~9 days, served from cache)."""
    try:
        return await _fetch_forecast()
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# -- rain radar (RainViewer) + map tiles (OpenStreetMap) ---------------------
#
# SECURITY — the tile endpoints fetch from the internet on behalf of the
# browser, so every part of the outgoing URL is pinned or validated:
#
# * The **hosts** are module constants. Nothing from the request ever
#   selects a host, so there is no SSRF surface.
# * **z/x/y** are parsed with a strict digits-only regex (no "+1", no
#   "1e3", no underscores, no whitespace) and then checked against
#   ALLOWED_ZOOMS and a tile window around Munich — exactly the zoom
#   levels and tiles the frontend can display. Everything else is 400 and
#   never causes an upstream request.
# * The **radar frame id** is looked up in the frame list RainViewer
#   itself last delivered; only the path from that list is used. A frame
#   id we have not seen is 404 — a client can never inject a path.
# * The response is only relayed when upstream declares an image type,
#   and is capped in size. It is served with nosniff.
#
# We also cache tiles server side: OSM's tile usage policy and
# RainViewer's terms both ask for modest request rates, and the kiosk
# re-requests the same tiles constantly.

RAINVIEWER_INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"
RAINVIEWER_TILE_HOST = "https://tilecache.rainviewer.com"
OSM_TILE_HOST = "https://tile.openstreetmap.org"

# Zoom levels the frontend can display, and the radar's default.
#
# RainViewer's free radar tiles only exist up to zoom 7 (deeper zooms
# return a "Zoom Level Not Supported" placeholder), so the radar uses 5-7
# and draws its tiles at double size. The base map is fetched one level
# deeper (6-8) at normal size so it stays crisp over the same ground —
# hence one allowlist spanning both, see app/static/js/weather-map.js.
ALLOWED_ZOOMS = (5, 6, 7, 8)
DEFAULT_ZOOM = 7
# Half-width of the accepted tile window around Munich's own tile. The
# viewport is centred on Munich and needs at most ~4 tiles in each
# direction at the base map's tile size; beyond that is not displayable.
MAX_TILE_RADIUS = 4

RADAR_FRAMES_CACHE_TTL_SECONDS = 300.0
# Frames arrive every ~10 minutes; ten of them are the last ~100 minutes.
MAX_RADAR_FRAMES = 10
BASE_TILE_CACHE_TTL_SECONDS = 24 * 3600.0
RADAR_TILE_CACHE_TTL_SECONDS = 300.0
# A 256px map tile is a few dozen KB; this is a generous ceiling.
MAX_TILE_BYTES = 512 * 1024
# Bounded number of cached tiles (a full 5x3 grid per zoom and frame).
MAX_CACHED_TILES = 300
TILE_REQUEST_TIMEOUT_SECONDS = 10.0

# Strict integer syntax for path segments: digits only, no sign, no
# separators. int() alone would accept " 12", "+12" and even "1_2".
_DIGITS = re.compile(r"^[0-9]{1,12}$")


def create_rainviewer_client() -> httpx.AsyncClient:
    """HTTP client for the RainViewer frame index."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def create_tile_client(base_url: str) -> httpx.AsyncClient:
    """HTTP client for a fixed tile host (OSM or RainViewer).

    ``base_url`` is always one of the module constants — never a value
    derived from a request.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"User-Agent": USER_AGENT},
        timeout=TILE_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def munich_tile(zoom: int) -> tuple[int, int]:
    """Slippy-map tile (x, y) containing Munich at ``zoom``."""
    scale = 2**zoom
    x = int((MUNICH_LON + 180.0) / 360.0 * scale)
    lat_rad = math.radians(MUNICH_LAT)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale)
    return x, y


def is_allowed_tile(zoom: int, x: int, y: int) -> bool:
    """Whether (zoom, x, y) is inside the displayable window around Munich."""
    if zoom not in ALLOWED_ZOOMS:
        return False
    scale = 2**zoom
    if not (0 <= x < scale and 0 <= y < scale):
        return False
    center_x, center_y = munich_tile(zoom)
    return abs(x - center_x) <= MAX_TILE_RADIUS and abs(y - center_y) <= MAX_TILE_RADIUS


def _parse_index(raw: str) -> int | None:
    """A non-negative int from a strict digits-only path segment, else None."""
    return int(raw) if _DIGITS.fullmatch(raw) else None


def _validated_coords(z: str, x: str, y: str) -> tuple[int, int, int]:
    """Parse and range-check tile coordinates, or raise HTTP 400."""
    zoom, tile_x, tile_y = (_parse_index(z), _parse_index(x), _parse_index(y))
    if zoom is None or tile_x is None or tile_y is None:
        raise HTTPException(status_code=400, detail="Ungültige Kachel-Koordinaten.")
    if not is_allowed_tile(zoom, tile_x, tile_y):
        raise HTTPException(status_code=400, detail="Kachel außerhalb des erlaubten Bereichs.")
    return zoom, tile_x, tile_y


# -- radar frame list -------------------------------------------------------

# Frame id (epoch seconds) → the path RainViewer published for it. This map
# is the ONLY source of radar tile paths; a client-supplied frame that is
# not in it is refused.
_radar_paths: dict[int, str] = {}
_radar_frames: list[dict] = []
_radar_valid_until = 0.0
_radar_lock = asyncio.Lock()


def _reset_radar_cache() -> None:
    global _radar_valid_until
    _radar_paths.clear()
    _radar_frames.clear()
    _radar_valid_until = 0.0


def parse_radar_index(data: object) -> list[tuple[int, str]]:
    """RainViewer's weather-maps.json → ``[(time, path), ...]`` (oldest first).

    Only the most recent ``MAX_RADAR_FRAMES`` past frames are kept — that
    is the animation window. Malformed entries are skipped individually.
    """
    if not isinstance(data, dict):
        return []
    radar = data.get("radar")
    past = radar.get("past") if isinstance(radar, dict) else None
    if not isinstance(past, list):
        return []
    frames: list[tuple[int, str]] = []
    for entry in past:
        if not isinstance(entry, dict):
            continue
        moment = entry.get("time")
        path = entry.get("path")
        # bool is an int subclass — exclude it explicitly.
        if isinstance(moment, bool) or not isinstance(moment, int) or moment <= 0:
            continue
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        frames.append((moment, path))
    frames.sort(key=lambda frame: frame[0])
    return frames[-MAX_RADAR_FRAMES:]


async def _fetch_radar_frames_uncached() -> list[tuple[int, str]]:
    async with create_rainviewer_client() as client:
        try:
            response = await client.send(
                client.build_request("GET", RAINVIEWER_INDEX_URL), stream=True
            )
            try:
                if response.status_code != 200:
                    raise WeatherUnavailableError(
                        "Der Regenradar-Dienst antwortet mit Fehler"
                        f" (HTTP {response.status_code})."
                    )
                body = await _read_limited(
                    response,
                    MAX_FORECAST_RESPONSE_BYTES,
                    "Der Regenradar-Dienst antwortet mit einer zu großen Antwort.",
                )
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise WeatherUnavailableError("Der Regenradar-Dienst ist nicht erreichbar.") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WeatherUnavailableError(
            "Der Regenradar-Dienst liefert eine unlesbare Antwort."
        ) from exc
    frames = parse_radar_index(data)
    if not frames:
        raise WeatherUnavailableError("Der Regenradar liefert derzeit keine Bilder.")
    return frames


async def _ensure_radar_frames() -> list[dict]:
    """The cached frame list, refetched when stale (lock-protected)."""
    global _radar_valid_until
    if _radar_frames and _now() < _radar_valid_until:
        return _radar_frames
    async with _radar_lock:
        if _radar_frames and _now() < _radar_valid_until:
            return _radar_frames
        frames = await _fetch_radar_frames_uncached()
        _radar_paths.clear()
        _radar_frames.clear()
        for moment, path in frames:
            _radar_paths[moment] = path
            _radar_frames.append({"id": moment, "t": moment * 1000})
        _radar_valid_until = _now() + RADAR_FRAMES_CACHE_TTL_SECONDS
        return _radar_frames


@router.get("/radar/frames")
async def get_radar_frames() -> dict:
    """Timestamps of the radar images available for the animation."""
    try:
        return {"frames": await _ensure_radar_frames()}
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# -- tile proxy -------------------------------------------------------------

# url → (bytes, content_type, valid_until). Insertion-ordered, so the
# oldest entries are evicted first once MAX_CACHED_TILES is reached.
_tile_cache: dict[str, tuple[bytes, str, float]] = {}


def _cached_tile(url: str) -> tuple[bytes, str] | None:
    entry = _tile_cache.get(url)
    if entry is None or _now() >= entry[2]:
        return None
    return entry[0], entry[1]


def _store_tile(url: str, body: bytes, content_type: str, ttl: float) -> None:
    _tile_cache[url] = (body, content_type, _now() + ttl)
    while len(_tile_cache) > MAX_CACHED_TILES:
        _tile_cache.pop(next(iter(_tile_cache)))


async def _fetch_tile(host: str, path: str, ttl: float) -> tuple[bytes, str]:
    """Fetch (or serve from cache) one tile from a fixed upstream host."""
    url = f"{host}{path}"
    cached = _cached_tile(url)
    if cached is not None:
        return cached
    async with create_tile_client(host) as client:
        try:
            response = await client.send(client.build_request("GET", url), stream=True)
            try:
                if response.status_code != 200:
                    raise WeatherUnavailableError(
                        f"Der Kartendienst antwortet mit Fehler (HTTP {response.status_code})."
                    )
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                # Never relay a non-image body: proxied HTML/JS would be
                # same-origin content in the browser.
                if not content_type.startswith("image/"):
                    raise WeatherUnavailableError(
                        "Der Kartendienst liefert kein Kachelbild."
                    )
                body = await _read_limited(
                    response, MAX_TILE_BYTES, "Der Kartendienst liefert eine zu große Kachel."
                )
            finally:
                await response.aclose()
        except httpx.HTTPError as exc:
            raise WeatherUnavailableError("Der Kartendienst ist nicht erreichbar.") from exc
    _store_tile(url, body, content_type, ttl)
    return body, content_type


def _tile_response(body: bytes, content_type: str, max_age: float) -> Response:
    """Relay a tile with its own caching policy (see API_CACHE_EXEMPT_PREFIXES)."""
    return Response(
        content=body,
        media_type=content_type,
        headers={
            "Cache-Control": f"private, max-age={int(max_age)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/tile/base/{z}/{x}/{y}")
async def get_base_tile(z: str, x: str, y: str) -> Response:
    """One OpenStreetMap base map tile (proxied, cached, range-checked)."""
    zoom, tile_x, tile_y = _validated_coords(z, x, y)
    try:
        body, content_type = await _fetch_tile(
            OSM_TILE_HOST, f"/{zoom}/{tile_x}/{tile_y}.png", BASE_TILE_CACHE_TTL_SECONDS
        )
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _tile_response(body, content_type, BASE_TILE_CACHE_TTL_SECONDS)


@router.get("/tile/radar/{frame}/{z}/{x}/{y}")
async def get_radar_tile(frame: str, z: str, x: str, y: str) -> Response:
    """One RainViewer radar tile for a frame from RainViewer's own list."""
    zoom, tile_x, tile_y = _validated_coords(z, x, y)
    frame_id = _parse_index(frame)
    if frame_id is None:
        raise HTTPException(status_code=400, detail="Ungültige Radar-Zeitmarke.")
    try:
        await _ensure_radar_frames()
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    frame_path = _radar_paths.get(frame_id)
    if frame_path is None:
        raise HTTPException(status_code=404, detail="Unbekannte Radar-Zeitmarke.")
    try:
        body, content_type = await _fetch_tile(
            RAINVIEWER_TILE_HOST,
            f"{frame_path}/256/{zoom}/{tile_x}/{tile_y}/2/1_1.png",
            RADAR_TILE_CACHE_TTL_SECONDS,
        )
    except WeatherUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _tile_response(body, content_type, RADAR_TILE_CACHE_TTL_SECONDS)
