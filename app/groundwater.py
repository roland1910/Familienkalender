"""Groundwater view API (/api/groundwater): monitoring stations around Munich.

Data source is the GKD Bayern (Bayerisches Landesamt für Umwelt). There is
**no official API**: the overview pages for the upper and the deeper aquifer
carry the whole station list as an inline JSON array inside their map
bootstrap (``LfUMap.init({… "pointer": [ … ]})``), and that array is what we
read. The low-water classification comes from the NID (Niedrigwasser-
Informationsdienst) page, which embeds the same shape and whose
``pegelnummer`` is the very GKD station number — that is the join key. The
NID's own coordinates are Gauß-Krüger and are ignored; the geometry comes
from the GKD.

**This is scraping.** A relaunch of either page can break the extraction at
any time, so every parse step is defensive: a page without the array yields
no stations instead of raising, individual broken entries are skipped and
counted, and a failed run never blanks what is already stored (see
``Storage.replace_groundwater_stations`` — the same lesson as the photo index
in Etappe 31 and the mirror sync in Etappe 41b: availability, not emptiness,
is the criterion).

Hardening mirrors ``app.weather``: the target hosts are module constants
(nothing from a request ever selects a host, so there is no SSRF surface), a
request timeout, a hard size limit on the **streamed** body (the big GKD page
arrives chunked WITHOUT a Content-Length, so a declared length must not be
what the cap depends on), a lock against a thundering herd, and German error
messages surfaced as HTTP 502.

Unlike MET Norway there is nothing to make a conditional request with: the
pages answer ``Cache-Control: no-store`` and carry neither ETag nor
Last-Modified. The cache is therefore purely time based — the station list is
refetched once a day, driven by the periodic sync (see app.sync, gated behind
GROUNDWATER_REFRESH=1 so no test ever scrapes) and, on demand, by the first
request to ``/api/groundwater/stations``.
"""

import asyncio
import datetime as dt
import json
import logging
import math
import os
import re
from collections.abc import Iterable

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.models import LOCAL_TZ, GroundwaterStation
from app.power import downsample
from app.sanitize import sanitize_error
from app.settings import get_groundwater_status, set_groundwater_status
from app.storage import Storage, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/groundwater")

# Munich (city centre) and the radius of the view. Fixed constants — never
# user-supplied. 50 km covers the whole metropolitan area (~300 stations).
MUNICH_LAT = 48.1374
MUNICH_LON = 11.5755
RADIUS_KM = 50.0

# A reading older than this counts as stale. Of the ~300 stations in the
# radius only ~165 are actually maintained; the rest have been silent for
# years (Bavaria-wide the oldest entry dates from 1947), and a dot on the map
# claiming a water level from 1947 would be worse than no dot at all.
STALE_DAYS = 7

GKD_HOST = "https://www.gkd.bayern.de"
# "upper" = oberes Stockwerk (~2000 stations), "deep" = tieferes Stockwerke
# (~260). Both pages have the identical shape.
GKD_TIER_PATHS = {
    "upper": "/de/grundwasser/oberesstockwerk",
    "deep": "/de/grundwasser/tieferestockwerke",
}
NID_URL = "https://www.nid.bayern.de/grundwasser"

# Same descriptive User-Agent as the weather client: a public authority's
# server should be able to see who is asking and how to reach us.
USER_AGENT = "Familienkalender/1.0 github.com/roland1910/Familienkalender"

REQUEST_TIMEOUT_SECONDS = 20.0
# The upper-aquifer page is ~600 KB; 8 MB is a generous ceiling that still
# stops a runaway response from being buffered in full.
MAX_PAGE_BYTES = 8 * 1024 * 1024

# Groundwater levels move over weeks, and the GKD publishes new readings
# roughly daily — refetching once a day is plenty and polite.
REFRESH_INTERVAL_SECONDS = 24 * 3600.0
# After a failure we wait an hour instead of retrying with every sync tick
# (the sync runs every five minutes, which would mean ~290 requests a day at
# a service that is already having a bad time) — and still recover same-day.
ERROR_RETRY_SECONDS = 3600.0

# NID low-water classes: 0 = kein Niedrigwasser … 3 = neuer Niedrigstwert.
# The page also uses -2 / "Keine Klassifizierung" for stations it does not
# rate; that is the absence of a classification, not a class, so such an
# entry is dropped rather than handed to the frontend as a colour.
SITUATION_CLASSES = (0, 1, 2, 3)

# Sparkline windows offered by the view, and the default. Clamped exactly
# like ``hours`` in /api/power/history: anything else falls back to the
# default rather than being honoured or rejected.
SPARKLINE_ALLOWED_DAYS = (30, 90, 365)
SPARKLINE_DEFAULT_DAYS = 90
# A marker sparkline is about 40 px wide on the kiosk — it cannot show more
# than a few dozen points, and 165 stations x a year of daily values would
# be a payload of ~60k numbers for nothing.
MAX_SPARKLINE_POINTS = 40

# Strict integer syntax for the station number in the history path: ASCII
# digits only, no sign, no separators, no full-width digits. int() alone
# would accept " 12", "+12", "1_2" and full-width digits (same rationale
# as the tile proxy's _DIGITS in app.weather).
_DIGITS = re.compile(r"^[0-9]{1,12}$")

# The station array sits behind this key in the map bootstrap of both sites.
_POINTER_KEY = '"pointer":'

# The GKD publishes its timestamps as local Europe/Berlin wall time.
_GKD_TIME_FORMAT = "%d.%m.%Y %H:%M"

# Serializes refresh runs: the periodic sync and a first page load must not
# scrape both pages twice in parallel.
_refresh_lock = asyncio.Lock()


class GroundwaterUnavailableError(Exception):
    """The groundwater source cannot deliver right now.

    The message is German and ends up verbatim in the admin status and — when
    there is nothing stored to fall back on — in the 502 the frontend shows.
    """


# -- parsing ----------------------------------------------------------------


def _pointer_array(html: str) -> list:
    """The inline ``"pointer": [...]`` array of a GKD/NID page, or ``[]``.

    Deliberately total: a relaunched page, a maintenance notice or a truncated
    body all yield an empty list instead of an exception. The caller turns
    "no stations at all" into a failure; a single unreadable page must never
    propagate a parser exception into the request handler.
    """
    index = html.find(_POINTER_KEY)
    if index < 0:
        return []
    start = html.find("[", index + len(_POINTER_KEY))
    if start < 0:
        return []
    try:
        value, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError:
        logger.warning("Groundwater page carries an unreadable station array")
        return []
    return value if isinstance(value, list) else []


def parse_decimal(raw: object) -> float | None:
    """A float from the GKD's German decimal notation, else ``None``.

    The pages use a decimal COMMA, and a missing value is the literal string
    ``"--"`` (not an empty field) — an unguarded float() would blow up on it.
    """
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        number = float(raw)
        return number if math.isfinite(number) else None
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(",", ".")
    if not text or text == "--":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_measured_at(raw: object) -> str | None:
    """``"TT.MM.JJJJ HH:MM"`` (local Europe/Berlin) → ISO-8601 UTC, else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        naive = dt.datetime.strptime(raw.strip(), _GKD_TIME_FORMAT)
    except ValueError:
        return None
    return naive.replace(tzinfo=LOCAL_TZ).astimezone(dt.UTC).isoformat()


def parse_station_list(html: str, tier: str) -> list[GroundwaterStation]:
    """A GKD overview page → its monitoring stations (see the module docstring).

    Broken entries are skipped individually and only counted in the log — the
    page content is foreign data and has no business appearing verbatim in our
    logs. Missing or unreadable measurements become ``None`` rather than
    dropping the station: its position and name are still worth showing.
    """
    stations: list[GroundwaterStation] = []
    skipped = 0
    for entry in _pointer_array(html):
        station = _station_from_entry(entry, tier)
        if station is None:
            skipped += 1
            continue
        stations.append(station)
    if skipped:
        logger.warning("Skipped %d unreadable groundwater entries (%s)", skipped, tier)
    return stations


def _station_from_entry(entry: object, tier: str) -> GroundwaterStation | None:
    """One pointer object → a station, or ``None`` when it is unusable.

    Number, name and both coordinates are mandatory: without them the station
    can neither be identified nor drawn.
    """
    if not isinstance(entry, dict):
        return None
    number = entry.get("p")
    name = entry.get("n")
    lat = parse_decimal(entry.get("lat"))
    lon = parse_decimal(entry.get("lon"))
    if not isinstance(number, str) or not number:
        return None
    if not isinstance(name, str) or not name:
        return None
    if lat is None or lon is None:
        return None
    aquifer = entry.get("gwl")
    uri = entry.get("uri")
    return GroundwaterStation(
        number=number,
        name=name,
        lat=lat,
        lon=lon,
        tier=tier,
        # Never built from the name: the path segment in "uri" is the river
        # basin, not the town (Johanneskirchen is in Munich but files under
        # "kelheim"), so the URL is taken over verbatim or left empty.
        uri=uri if isinstance(uri, str) else "",
        aquifer=aquifer if isinstance(aquifer, str) else "",
        level_m_nn=parse_decimal(entry.get("w")),
        depth_m=parse_decimal(entry.get("w2")),
        measured_at=parse_measured_at(entry.get("d")),
    )


def parse_nid_situations(html: str) -> dict[str, tuple[str, int]]:
    """The NID page → ``{station number: (situation text, class)}``.

    Only stations with a real classification (``SITUATION_CLASSES``) are
    returned; everything else is simply absent from the mapping, which the
    join reads as "no rating for this station".
    """
    result: dict[str, tuple[str, int]] = {}
    for entry in _pointer_array(html):
        if not isinstance(entry, dict):
            continue
        number = entry.get("pegelnummer")
        situation = entry.get("situation")
        klasse = entry.get("klasse")
        if not isinstance(number, str) or not number:
            continue
        if not isinstance(situation, str) or not situation:
            continue
        if isinstance(klasse, bool) or not isinstance(klasse, int):
            continue
        if klasse not in SITUATION_CLASSES:
            continue
        result[number] = (situation, klasse)
    return result


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres (math only — no new dependency)."""
    earth_radius_km = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def is_near_munich(lat: float, lon: float) -> bool:
    """Whether a position is inside the view's radius around Munich."""
    return haversine_km(MUNICH_LAT, MUNICH_LON, lat, lon) <= RADIUS_KM


def is_stale(measured_at: str | None, now: dt.datetime) -> bool:
    """Whether a reading is older than STALE_DAYS (or has no usable date).

    An unreadable timestamp counts as stale on purpose: a value we cannot
    date is a value we cannot vouch for.
    """
    if not measured_at:
        return True
    try:
        moment = dt.datetime.fromisoformat(measured_at)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return (now - moment) > dt.timedelta(days=STALE_DAYS)


# -- fetching ---------------------------------------------------------------


def create_gkd_client() -> httpx.AsyncClient:
    """HTTP client for the GKD (fixed host, descriptive User-Agent)."""
    return httpx.AsyncClient(
        base_url=GKD_HOST,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def create_nid_client() -> httpx.AsyncClient:
    """HTTP client for the NID low-water pages (fixed host)."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


async def _read_limited(response: httpx.Response, limit: int, message: str) -> bytes:
    """Read a streamed body under a hard size cap.

    The GKD pages arrive chunked without a Content-Length, so counting the
    streamed chunks is the actual guard; the declared length is only a cheap
    early exit when it is present and honest.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise GroundwaterUnavailableError(message)
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise GroundwaterUnavailableError(message)
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_page(client: httpx.AsyncClient, url: str, label: str) -> str:
    """One page as text, with timeout, status check and size cap.

    Public because app.groundwater_history fetches the per-station table
    pages with exactly the same hardening — duplicating the streaming
    size guard would be the one place where the two could drift apart.
    """
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
        try:
            if response.status_code != 200:
                raise GroundwaterUnavailableError(
                    f"{label} antwortet mit Fehler (HTTP {response.status_code})."
                )
            body = await _read_limited(
                response,
                MAX_PAGE_BYTES,
                f"{label} antwortet mit einer zu großen Seite.",
            )
        finally:
            await response.aclose()
    except httpx.HTTPError as exc:
        raise GroundwaterUnavailableError(f"{label} ist nicht erreichbar.") from exc
    # The pages declare charset=utf-8; a stray byte must not cost the whole
    # list, so decoding is lenient and the parser skips what it cannot read.
    return body.decode("utf-8", errors="replace")


async def _fetch_situations() -> dict[str, tuple[str, int]]:
    """The NID classification, or an empty mapping when it is unavailable.

    The rating is an EXTRA on top of the station list; a broken NID must
    never cost the view its stations, so failures are logged and swallowed.
    """
    try:
        async with create_nid_client() as client:
            page = await fetch_page(client, NID_URL, "Der Niedrigwasserdienst")
        return parse_nid_situations(page)
    except GroundwaterUnavailableError as exc:
        logger.warning("Low-water classification unavailable: %s", exc)
        return {}
    except Exception:  # pragma: no cover - defensive, the rating is optional
        logger.exception("Unexpected error reading the low-water classification")
        return {}


async def fetch_stations() -> list[GroundwaterStation]:
    """Both GKD pages → the stations inside the radius, NID rating joined in.

    Raises ``GroundwaterUnavailableError`` when no station could be read at
    all — including the "the pages changed" case, because for the protection
    rule an unreadable page must look like a failure, never like "there are
    no stations any more".
    """
    stations: list[GroundwaterStation] = []
    async with create_gkd_client() as client:
        for tier, path in GKD_TIER_PATHS.items():
            page = await fetch_page(client, path, "Der Grundwasserdienst")
            stations.extend(
                station
                for station in parse_station_list(page, tier)
                if is_near_munich(station.lat, station.lon)
            )
    if not stations:
        raise GroundwaterUnavailableError(
            "Der Grundwasserdienst liefert derzeit keine Messstellen."
        )
    rating = await _fetch_situations()
    return [_with_situation(station, rating.get(station.number)) for station in stations]


def daily_readings(
    stations: Iterable[GroundwaterStation],
) -> dict[str, tuple[str, float]]:
    """Each station's current value as a daily reading: ``{number: (day, m ü. NN)}``.

    This is what keeps the history growing without ever scraping a table page
    again: the station list is fetched once a day anyway and carries the
    latest level, so filing it under its measurement day costs no extra
    request. The day is the LOCAL (Europe/Berlin) date of ``measured_at`` —
    the stored timestamp is UTC, and a reading published for 00:30 local time
    belongs to that local day, not to the previous UTC one.

    Stations without a readable value or without a readable timestamp are
    left out: a value we cannot date is a value we cannot file.
    """
    result: dict[str, tuple[str, float]] = {}
    for station in stations:
        if station.level_m_nn is None or not station.measured_at:
            continue
        try:
            moment = dt.datetime.fromisoformat(station.measured_at)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        result[station.number] = (
            moment.astimezone(LOCAL_TZ).date().isoformat(),
            station.level_m_nn,
        )
    return result


def _store_daily_readings(storage: Storage, stations: list[GroundwaterStation]) -> None:
    """File the current value of every station as that day's reading.

    Isolated: the history is a nice-to-have on top of the station list, and
    a failure here must never cost the refresh its stations. Writing is an
    upsert on (number, day), so several runs on the same day can neither
    duplicate nor drift.
    """
    try:
        rows = [
            (number, day, level) for number, (day, level) in daily_readings(stations).items()
        ]
        storage.add_groundwater_readings(rows)
    except Exception:  # pragma: no cover - defensive, the history is extra
        logger.exception("Failed to file the daily groundwater readings")


def _with_situation(
    station: GroundwaterStation, rating: tuple[str, int] | None
) -> GroundwaterStation:
    if rating is None:
        return station
    situation, situation_class = rating
    return GroundwaterStation(
        number=station.number,
        name=station.name,
        lat=station.lat,
        lon=station.lon,
        tier=station.tier,
        aquifer=station.aquifer,
        level_m_nn=station.level_m_nn,
        depth_m=station.depth_m,
        measured_at=station.measured_at,
        uri=station.uri,
        situation=situation,
        situation_class=situation_class,
    )


# -- refresh ----------------------------------------------------------------


def _refresh_due(storage: Storage, now: dt.datetime) -> bool:
    """Whether the station list should be refetched.

    The decision runs off the PERSISTED status rather than an in-memory
    timer: the Pi restarts often enough that an in-process cache would mean
    a fresh scrape on every boot. After an error the retry window is much
    shorter than the daily interval (see ERROR_RETRY_SECONDS).

    Deliberately NOT "due whenever the table is empty": with the source down
    the table stays empty, and that shortcut would scrape both pages on every
    five-minute sync tick. A never-run install has no status at all, which is
    the case that covers a genuinely cold start — and a successful run always
    stores at least one station (an empty result is a failure, see
    fetch_stations), so a filled status with an empty table cannot occur.
    """
    status = get_groundwater_status(storage)
    last_run = status.get("last_run")
    if not last_run:
        return True
    try:
        previous = dt.datetime.fromisoformat(last_run)
    except (TypeError, ValueError):
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.UTC)
    age = (now - previous).total_seconds()
    if age < 0:  # clock jumped backwards — wait rather than hammer
        return False
    interval = ERROR_RETRY_SECONDS if status.get("error") else REFRESH_INTERVAL_SECONDS
    return age >= interval


def periodic_refresh_enabled() -> bool:
    """Whether the periodic sync scrapes the GKD (disabled for tests/dev).

    Off unless ``GROUNDWATER_REFRESH=1`` (set by run.sh), exactly like the
    periodic photo scan: every unit test that exercises ``sync_all`` would
    otherwise fire real requests at a public authority's website. The
    ``/api/groundwater/stations`` endpoint refreshes on demand regardless, so
    the view works on a dev machine too — it just does not scrape in the
    background there.
    """
    return os.environ.get("GROUNDWATER_REFRESH") == "1"


async def refresh_stations(
    storage: Storage, *, now: dt.datetime | None = None, force: bool = False
) -> int:
    """Refetch the station list when it is due; returns the stations stored.

    Never raises: a failure is sanitized into the ``groundwater_status``
    setting and the previously stored stations stay exactly as they were
    (protection rule) — and, just as importantly, the readings table is not
    touched on a failure either. A successful run additionally files each
    station's current level as that day's reading (see _store_daily_readings).
    """
    moment = now or dt.datetime.now(dt.UTC)
    async with _refresh_lock:
        # Another caller may have refreshed while we waited for the lock.
        if not force and not _refresh_due(storage, moment):
            return storage.count_groundwater_stations()
        try:
            stations = await fetch_stations()
        except GroundwaterUnavailableError as exc:
            error = sanitize_error(str(exc))
            logger.warning("Groundwater refresh failed: %s", error)
            stored = storage.count_groundwater_stations()
            set_groundwater_status(
                storage, last_run=moment.isoformat(), stations=stored, error=error
            )
            return stored
        except Exception as exc:  # pragma: no cover - defensive
            error = sanitize_error(str(exc))
            logger.exception("Unexpected error refreshing the groundwater stations")
            stored = storage.count_groundwater_stations()
            set_groundwater_status(
                storage, last_run=moment.isoformat(), stations=stored, error=error
            )
            return stored
        stored = storage.replace_groundwater_stations(stations)
        # The list we just fetched carries every station's current level —
        # filing it as that day's reading is what makes the history grow on
        # its own, with no further request (see app.groundwater_history).
        _store_daily_readings(storage, stations)
        set_groundwater_status(
            storage, last_run=moment.isoformat(), stations=stored, error=None
        )
        logger.info("Groundwater: %d stations within %.0f km", stored, RADIUS_KM)
        return stored


# -- API --------------------------------------------------------------------


def _station_payload(station: GroundwaterStation, now: dt.datetime) -> dict:
    """One station as the frontend sees it (measured_at in epoch ms)."""
    stale = is_stale(station.measured_at, now)
    return {
        "number": station.number,
        "name": station.name,
        "lat": station.lat,
        "lon": station.lon,
        "aquifer": station.aquifer,
        "tier": station.tier,
        # Water table above sea level — this is what the history curve plots.
        "level_m_nn": station.level_m_nn,
        # Depth below ground (Flurabstand). Runs OPPOSITE to level_m_nn, so it
        # belongs in the detail readout, never on the same chart axis.
        "depth_m": station.depth_m,
        "measured_at": _epoch_ms(station.measured_at),
        "situation": station.situation,
        "situation_class": station.situation_class,
        "stale": stale,
    }


def _epoch_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        moment = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp() * 1000)


@router.get("/stations")
async def get_stations(stale: bool = False) -> dict:
    """The monitoring stations around Munich with their latest reading.

    Stale stations (no reading for STALE_DAYS) are withheld unless
    ``?stale=true`` asks for them — about half the stations in the radius are
    long abandoned, and a dot claiming a 1947 water level is worse than none.

    Served from the database; the daily refetch happens here only when the
    list is due (or the database is still empty on a fresh install). When the
    upstream is down but stations are stored, the stored ones are served —
    stale data beats an empty map. Only with nothing at all to show does this
    become a 502 with the German message.
    """
    storage = get_storage()
    now = dt.datetime.now(dt.UTC)
    await refresh_stations(storage, now=now)
    stations = storage.list_groundwater_stations()
    if not stations:
        raise HTTPException(status_code=502, detail=_unavailable_detail(storage))
    payload = [_station_payload(station, now) for station in stations]
    if not stale:
        payload = [item for item in payload if not item["stale"]]
    return {"stations": payload}


def _unavailable_detail(storage: Storage) -> str:
    """The German error for an empty station list (last error if we have one)."""
    error = get_groundwater_status(storage).get("error")
    return error or "Der Grundwasserdienst ist nicht erreichbar."


@router.get("/history/{number}")
async def get_history(number: str) -> dict:
    """The stored daily water levels (m ü. NN) of one station, oldest first.

    SECURITY: ``number`` comes from the client. It is parsed as a strict
    digit string and then used ONLY as a lookup key against our own database
    — it is never interpolated into an outgoing URL, and an invalid value is
    a 400 that causes no upstream request whatsoever.
    """
    if not _DIGITS.fullmatch(number):
        raise HTTPException(status_code=400, detail="Ungültige Messstellennummer.")
    storage = get_storage()
    if storage.get_groundwater_station(number) is None:
        raise HTTPException(status_code=404, detail="Unbekannte Messstelle.")
    points = [
        {"t": _day_epoch_ms(day), "v": level}
        for day, level in storage.list_groundwater_readings(number)
    ]
    return {"number": number, "points": [point for point in points if point["t"] is not None]}


def clamp_sparkline_days(days: int) -> int:
    """Keep the requested window inside the offered set, else the default."""
    return days if days in SPARKLINE_ALLOWED_DAYS else SPARKLINE_DEFAULT_DAYS


@router.get("/sparklines")
async def get_sparklines(days: str = Query(default=str(SPARKLINE_DEFAULT_DAYS))) -> dict:
    """Every station's recent levels as bare value lists, for the map markers.

    Asking ``/history/{number}`` once per station would mean ~165 requests
    the moment the view opens, so the marker curves come from this one
    collected answer instead. Deliberately WITHOUT timestamps: a ~40 px
    sparkline plots its values evenly and has no room for a time axis; the
    detail view (one station, one request) is where the real curve lives.

    Purely a database read — no scraping happens here, however often the
    kiosk asks. Stations without any stored reading are simply absent from
    ``series``; the frontend draws their marker without a curve.
    """
    try:
        requested = int(days)
    except ValueError:
        requested = SPARKLINE_DEFAULT_DAYS
    window = clamp_sparkline_days(requested)
    since = (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=window)).isoformat()
    series = get_storage().groundwater_readings_since(since)
    return {
        "days": window,
        "series": {
            number: downsample(values, MAX_SPARKLINE_POINTS)
            for number, values in series.items()
        },
    }


def _day_epoch_ms(day: str) -> int | None:
    try:
        parsed = dt.date.fromisoformat(day)
    except ValueError:
        return None
    return int(dt.datetime.combine(parsed, dt.time.min, tzinfo=dt.UTC).timestamp() * 1000)
