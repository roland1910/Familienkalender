"""Filling the daily history of the groundwater stations (Etappe 46, path A).

Three things write into ``groundwater_readings``; all of them upsert on the
``(number, day)`` primary key, so re-reading a period can neither duplicate a
row nor change one:

1. **The daily station list.** ``app.groundwater.refresh_stations`` already
   fetches every station's current level once a day and files it as that
   day's reading — the history therefore grows on its own, for free.
2. **This module.** Each station has a plain HTML table page at
   ``<uri>/tabelle`` carrying roughly the last 62 daily values. That is the
   one-off starting stock behind the curve, and after a longer outage it is
   also what closes the gap. Note: the page takes NO period parameter —
   ``?zr=jahr``, ``?zr=gesamt``, ``?beginn=…&ende=…`` and ``?jahr=…`` were all
   tried against the live site and every one of them returns an empty page.
   Two months is what there is.
3. **:mod:`app.groundwater_import`** imports the full history (back into the
   1970s) from a ZIP Roland downloads BY HAND from the GKD download centre.
   There is deliberately no client for that form: its paths are disallowed in
   robots.txt and it requires a personal acceptance of the terms of use.

**The table URL is derived, never built.** The path segment inside a station
address is the river basin, not the town (Johanneskirchen is in Munich but
files under "kelheim"), so the only correct address is the ``uri`` the GKD
itself handed over with the station list, plus ``/tabelle``.

SECURITY: that ``uri`` comes from a scraped page, i.e. from foreign data, and
is the only part of an outgoing request that is not a module constant. Before
a single request is made it must therefore prove that it stays inside
``https://www.gkd.bayern.de/de/grundwasser/`` — same idea as
``CaldavWriteClient._ensure_own_url``. The raw prefix test alone is not
enough: a percent-encoded ``%2e%2e`` survives URL normalization and would
resolve one level up on the server, so the DECODED path is checked for dot
segments too (``has_dot_segment``, the trap from Etappe 41b). An address that
fails is skipped with a log line and causes NO request at all.

Politeness: one station page per second (``REQUEST_PAUSE_SECONDS``), at most
``MAX_STATIONS_PER_RUN`` stations per run, an hourly gate, and a hard stop
after ``MAX_CONSECUTIVE_ERRORS`` failures in a row. A single broken station
never ends the run. The whole thing runs behind the same ``GROUNDWATER_REFRESH``
env gate as the station scrape, so no unit test ever reaches the network.
"""

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser

import httpx

from app import groundwater
from app.models import LOCAL_TZ, GroundwaterStation, ReadingCoverage
from app.sanitize import sanitize_error
from app.settings import get_groundwater_history_status, set_groundwater_history_status
from app.storage import Storage
from app.url_validation import SourceURLError, has_dot_segment, validate_source_url

logger = logging.getLogger(__name__)

# Every station address must sit below this prefix — see the module docstring.
HISTORY_PREFIX = f"{groundwater.GKD_HOST}/de/grundwasser/"
# What turns a station's "Messwerte" page into its table of daily values.
TABLE_SUFFIX = "/tabelle"

# The table page dates its rows in German notation (the ISO dates in the
# download-centre CSV are a different format — see app.groundwater_import).
_GKD_DAY_FORMAT = "%d.%m.%Y"

# One second between two station pages. The full build walks ~165 stations,
# and a public authority's website should not see them arrive in a burst.
# Deliberately a constant and not a fraction of anything: tests set it to 0.
REQUEST_PAUSE_SECONDS = 1.0
# Stop a run rather than hammer a service that is clearly having a bad time.
# Counted CONSECUTIVELY: single broken stations exist and must not abort the
# whole build.
MAX_CONSECUTIVE_ERRORS = 5
# Ceiling per run so one sync tick can never spend more than about a minute
# on groundwater. The full build (~165 stations) therefore takes three runs,
# i.e. about three hours once — and then practically never again.
MAX_STATIONS_PER_RUN = 60
# How often a run may start at all. The periodic sync ticks every five
# minutes; without this gate a station whose page stays short would be
# refetched twelve times an hour.
BACKFILL_INTERVAL_SECONDS = 3600.0

# A station is refetched when its newest stored reading is older than this.
# The page covers ~62 days, so any gap up to two months can be closed
# completely; 14 days keeps the add-on from reacting to a single missed day.
GAP_DAYS = 14
# …and when the period its readings span is shorter than this. The COUNT
# would be the wrong measure: the daily station list writes one value per day
# and would, after the very first refresh, make every station look like it
# already had history. A station that only ever saw the daily path has a span
# of a few days; after a successful table fetch the span is ~61 days and the
# station is done. A page that really only offers a handful of values heals
# itself within a month of daily values instead of being refetched forever.
MIN_HISTORY_SPAN_DAYS = 30

# Serializes backfill runs (the periodic sync is the only caller today, but
# the same reasoning as app.groundwater's refresh lock applies).
_backfill_lock = asyncio.Lock()


# -- parsing the table page --------------------------------------------------


class _TableRowParser(HTMLParser):
    """Collects the cell texts of every ``<tr>`` of an HTML page.

    A real parser rather than a regex: cells carry nested markup
    (``<span>``/``<b>``) and the GKD's markup is not always closed properly,
    which a naive ``<td>(.*?)</td>`` would either swallow or mangle.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ARG002 - API
        if tag == "tr":
            self._flush_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._flush_cell()
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._flush_cell()
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        super().close()
        self._flush_row()

    def _flush_cell(self) -> None:
        if self._cell is None:
            return
        if self._row is not None:
            self._row.append("".join(self._cell).strip())
        self._cell = None

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None


def table_rows(html: str) -> list[list[str]]:
    """Every table row of a page as a list of cell texts (never raises)."""
    parser = _TableRowParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient by design
        logger.warning("A groundwater table page could not be parsed")
    return parser.rows


def parse_day(raw: object) -> str | None:
    """``"TT.MM.JJJJ"`` → ISO date string, else ``None``."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return dt.datetime.strptime(raw.strip(), _GKD_DAY_FORMAT).date().isoformat()
    except ValueError:
        return None


def parse_table_page(html: str) -> list[tuple[str, float]]:
    """A station's table page → ``[(ISO day, m ü. NN), …]``, oldest first.

    Deliberately total, exactly like the station-list parser: a relaunched
    page, a maintenance notice or a truncated body yield an empty list rather
    than an exception, and a single unreadable row is skipped instead of
    costing the other 61. The header row simply fails to parse as a date,
    which is why it needs no special case. The page is sorted newest first;
    the result is sorted the other way round because that is the order the
    chart and ``list_groundwater_readings`` use.
    """
    readings: dict[str, float] = {}
    skipped = 0
    for row in table_rows(html):
        if len(row) < 2:
            continue
        day = parse_day(row[0])
        level = groundwater.parse_decimal(row[1])
        if day is None or level is None:
            skipped += 1
            continue
        readings[day] = level
    if skipped:
        # Debug, not warning: the header row lands here on every single page.
        logger.debug("Skipped %d unreadable rows of a groundwater table page", skipped)
    return sorted(readings.items())


# -- deriving and validating the table URL -----------------------------------


def table_url(uri: str) -> str | None:
    """The table page of a stored station address, or ``None`` when unsafe.

    See the SECURITY paragraph of the module docstring: the address comes
    from a scraped page and must prove it stays inside the GKD's groundwater
    section before it is ever requested. A query or fragment is refused
    outright — appending a path segment to such an address would produce
    something quite different from what the stored URL looks like.
    """
    if not isinstance(uri, str):
        return None
    address = uri.strip()
    if not address or "?" in address or "#" in address:
        return None
    if not address.startswith(HISTORY_PREFIX) or len(address) <= len(HISTORY_PREFIX):
        return None
    if has_dot_segment(address):
        return None
    try:
        validate_source_url(address)
    except SourceURLError:
        return None
    address = address.rstrip("/")
    if address.endswith(TABLE_SUFFIX):
        return address
    return address + TABLE_SUFFIX


# -- which stations still need their history ---------------------------------


def _as_date(day: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def needs_history(
    station: GroundwaterStation,
    coverage: Mapping[str, ReadingCoverage],
    now: dt.datetime,
) -> bool:
    """Whether this station's table page should be fetched in this run.

    Three reasons to fetch: no readings at all, a gap of more than
    ``GAP_DAYS`` up to today, or a stored period shorter than
    ``MIN_HISTORY_SPAN_DAYS`` (the initial build — see the constant for why
    the span and not the row count decides).

    Two reasons never to fetch: an address that is not a safe GKD groundwater
    page, and a station whose own latest measurement is stale. About half the
    stations inside the radius have been silent for years; their table page
    holds nothing new, and asking for it every hour would be pure noise.
    """
    if table_url(station.uri) is None:
        return False
    if groundwater.is_stale(station.measured_at, now):
        return False
    entry = coverage.get(station.number)
    if entry is None:
        return True
    oldest = _as_date(entry.oldest)
    newest = _as_date(entry.newest)
    if oldest is None or newest is None:
        return True
    today = now.astimezone(LOCAL_TZ).date()
    if (today - newest).days > GAP_DAYS:
        return True
    return (newest - oldest).days < MIN_HISTORY_SPAN_DAYS


def stations_needing_history(
    stations: Iterable[GroundwaterStation],
    coverage: Mapping[str, ReadingCoverage],
    now: dt.datetime,
) -> list[GroundwaterStation]:
    """The stations of ``stations`` that want their table page fetched."""
    return [station for station in stations if needs_history(station, coverage, now)]


# -- fetching ----------------------------------------------------------------


def create_table_client() -> httpx.AsyncClient:
    """HTTP client for the per-station table pages (fixed host, same UA)."""
    return httpx.AsyncClient(
        headers={"User-Agent": groundwater.USER_AGENT, "Accept": "text/html"},
        timeout=groundwater.REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


async def _pause() -> None:
    """The politeness pause between two station pages."""
    await asyncio.sleep(REQUEST_PAUSE_SECONDS)


async def fetch_table(client: httpx.AsyncClient, url: str) -> list[tuple[str, float]]:
    """One station's daily values, with the station scrape's hardening.

    ``groundwater.fetch_page`` is reused deliberately (timeout, status check,
    streamed size cap) — a second copy of that guard is the one place where
    the two fetch paths could drift apart.
    """
    page = await groundwater.fetch_page(client, url, "Der Grundwasserdienst")
    return parse_table_page(page)


async def _backfill_station(
    client: httpx.AsyncClient, storage: Storage, station: GroundwaterStation
) -> int:
    """Fetch and store one station's table page; returns the rows written."""
    url = table_url(station.uri)
    if url is None:  # defensive: the selection already filtered these out
        logger.info(
            "Groundwater station %s skipped: its stored address is not a GKD "
            "groundwater page",
            station.number,
        )
        return 0
    readings = await fetch_table(client, url)
    if not readings:
        logger.info("Groundwater station %s: table page carries no values", station.number)
        return 0
    storage.upsert_groundwater_readings(station.number, readings)
    return len(readings)


def _backfill_due(storage: Storage, now: dt.datetime) -> bool:
    """Whether a backfill run may start (hourly gate, persisted).

    Off the PERSISTED status rather than an in-memory timer, for the same
    reason as the station refresh: the Pi restarts often enough that an
    in-process gate would mean a fresh run on every boot.
    """
    last_run = get_groundwater_history_status(storage).get("last_run")
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
    return age >= BACKFILL_INTERVAL_SECONDS


async def backfill_history(
    storage: Storage, *, now: dt.datetime | None = None, force: bool = False
) -> int:
    """Fetch the table page of every station that still lacks history.

    Returns the number of stations filled in this run. Never raises: a
    failure is sanitized into ``groundwater_history_status`` and whatever is
    already stored stays exactly as it is — readings are only ever added,
    never deleted, so a bad run can cost the curve nothing.
    """
    moment = now or dt.datetime.now(dt.UTC)
    async with _backfill_lock:
        # Another caller may have run while we waited for the lock.
        if not force and not _backfill_due(storage, moment):
            return 0
        due = stations_needing_history(
            storage.list_groundwater_stations(),
            storage.groundwater_reading_coverage(),
            moment,
        )[:MAX_STATIONS_PER_RUN]
        filled, readings, error = await _run_backfill(storage, due)
        set_groundwater_history_status(
            storage,
            last_run=moment.isoformat(),
            stations=filled,
            readings=readings,
            error=error,
        )
        if filled:
            logger.info(
                "Groundwater history: %d stations filled with %d readings", filled, readings
            )
        return filled


async def _run_backfill(
    storage: Storage, due: list[GroundwaterStation]
) -> tuple[int, int, str | None]:
    """Walk the due stations; returns (stations filled, rows, last error)."""
    if not due:
        return 0, 0, None
    filled = 0
    readings = 0
    consecutive = 0
    error: str | None = None
    async with create_table_client() as client:
        for index, station in enumerate(due):
            if index:
                await _pause()
            try:
                stored = await _backfill_station(client, storage, station)
            except groundwater.GroundwaterUnavailableError as exc:
                error = sanitize_error(str(exc))
                consecutive += 1
                logger.warning(
                    "Groundwater history for station %s failed: %s", station.number, error
                )
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "Groundwater history: stopping after %d consecutive errors",
                        consecutive,
                    )
                    break
                continue
            except Exception as exc:  # pragma: no cover - defensive
                error = sanitize_error(str(exc))
                consecutive += 1
                logger.exception("Unexpected error filling station %s", station.number)
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    break
                continue
            consecutive = 0
            if stored:
                filled += 1
                readings += stored
    return filled, readings, error
