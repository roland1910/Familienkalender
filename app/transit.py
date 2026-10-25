"""Live departure board for the kiosk (Etappe 52).

Shows the next two departures per block beside the slideshow: towards
Pasing on the left, the opposite direction on the right.

THE ENDPOINT IS UNOFFICIAL. MVG publishes no documented API; this is the
JSON the mvg.de website itself calls (`bgw-pt/v3`), confirmed live against
both stops on 25.10.2026. It can change without notice, so every parse
step below is defensive in the same way as the groundwater scraper: a
changed payload costs single rows, or at worst this one board — never the
view around it.

DIRECTION COMES FROM `lineId`, NOT FROM THE DESTINATION NAME. The fourth
segment of `swm:03143:G:H:016` is the classic MVV Hin-/Rückrichtung
marker. Matching destinations instead would be brittle twice over: the
same direction appears under several terminals (S3 towards Munich ends at
Holzkirchen *or* Deisenhofen), and the spelling varies within one payload
("Olympia-Einkaufsz." next to "Olympia-Einkaufszentrum"). Verified across
60 departures per stop: H/R split the directions cleanly.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transit")

MVG_BASE_URL = "https://www.mvg.de/api/bgw-pt/v3"
# Same courtesy as the MET Norway client: say who we are and how to be
# reached, so an operator can complain to us rather than just block us.
USER_AGENT = "Familienkalender/1.0 github.com/roland1910/Familienkalender"

REQUEST_TIMEOUT_SECONDS = 10.0
# Departures move minute by minute, but the kiosk polls far more often
# than that; 30 s keeps the board honest without hammering the endpoint.
DEPARTURES_CACHE_TTL_SECONDS = 30.0
# Short TTL for cached *errors* (same rationale as app.power and
# app.weather): a failing service is not re-hit by every poll, but a
# recovery shows up quickly.
ERROR_CACHE_TTL_SECONDS = 60.0
# A stop's departure list is a few KB; this cap only stops a runaway
# response from being buffered in full.
MAX_RESPONSE_BYTES = 1024 * 1024

# How many departures each block shows. Roland asked for two.
DEPARTURES_PER_BLOCK = 2
# Asked of the endpoint per stop — enough that two departures survive the
# per-line and per-direction filtering even at a busy stop.
FETCH_LIMIT = 40

STOP_ALTE_ALLEE = "de:09162:1804"
STOP_LANGWIED = "de:09162:1800"


@dataclass(frozen=True)
class Block:
    """One column entry: a line at a stop in one direction."""

    key: str
    side: str  # "left" (towards Pasing) or "right" (the other way)
    stop_id: str
    stop_name: str
    label: str  # the line as it is printed: "56", "143", "S3"
    direction: str  # "H" or "R", the lineId marker
    kind: str  # "bus" or "sbahn" — picks the badge in the frontend


# Fixed configuration, not admin-editable: Roland named these four exactly.
# Keeping it in code means the direction letters stay next to the comment
# that explains them.
BLOCKS: tuple[Block, ...] = (
    Block("bus56_pasing", "left", STOP_ALTE_ALLEE, "Alte Allee", "56", "H", "bus"),
    Block("s3_pasing", "left", STOP_LANGWIED, "Langwied", "S3", "H", "sbahn"),
    Block("bus56_back", "right", STOP_ALTE_ALLEE, "Alte Allee", "56", "R", "bus"),
    Block("bus143_back", "right", STOP_ALTE_ALLEE, "Alte Allee", "143", "H", "bus"),
)


class TransitUnavailableError(Exception):
    """The departure endpoint cannot deliver right now.

    The message is German and shown verbatim in the frontend.
    """


def _now() -> float:
    """Monotonic clock; wrapped so tests can control cache expiry."""
    return time.monotonic()


_payload: dict | None = None
_valid_until = 0.0
_error: str | None = None
_error_valid_until = 0.0
_lock = asyncio.Lock()


def reset_caches() -> None:
    """Drop the cached board and error (tests)."""
    global _payload, _valid_until, _error, _error_valid_until
    _payload = None
    _valid_until = 0.0
    _error = None
    _error_valid_until = 0.0


def create_mvg_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=MVG_BASE_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


async def _read_limited(response: httpx.Response) -> bytes:
    """Read a streamed body under a hard size cap.

    A missing or lying Content-Length must not bypass the cap, so the
    streamed chunks are counted as well.
    """
    message = "Abfahrtsdaten sind ungewöhnlich groß — Abruf abgebrochen."
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise TransitUnavailableError(message)
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise TransitUnavailableError(message)
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch_stop(client: httpx.AsyncClient, stop_id: str) -> list:
    """Raw departure list for one stop; never returns None."""
    try:
        request = client.build_request(
            "GET", "/departures", params={"globalId": stop_id, "limit": FETCH_LIMIT}
        )
        response = await client.send(request, stream=True)
        try:
            if response.status_code != 200:
                raise TransitUnavailableError(
                    f"Abfahrtsdaten nicht abrufbar (HTTP {response.status_code})."
                )
            body = await _read_limited(response)
        finally:
            await response.aclose()
    except httpx.HTTPError as exc:
        raise TransitUnavailableError(
            "Abfahrtsdaten: MVG ist nicht erreichbar."
        ) from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise TransitUnavailableError(
            "Abfahrtsdaten sind unlesbar (unerwartetes Format)."
        ) from exc
    # A changed endpoint that answers with an object instead of a list is
    # not an error we can act on — an empty board says the same thing
    # without taking the slideshow down with it.
    if not isinstance(data, list):
        logger.warning(
            "MVG departures for %s: expected a list, got %s",
            stop_id,
            type(data).__name__,
        )
        return []
    return data


def _direction_of(line_id: object) -> str | None:
    """The H/R marker from `swm:03143:G:H:016`, or None if unreadable."""
    if not isinstance(line_id, str):
        return None
    parts = line_id.split(":")
    return parts[3] if len(parts) > 3 else None


def parse_departure(entry: object) -> dict | None:
    """One departure, or None when the entry is unusable.

    Deliberately total: anything surprising returns None instead of
    raising, so a single changed row cannot empty the board.
    """
    if not isinstance(entry, dict):
        return None
    planned = entry.get("plannedDepartureTime")
    live = entry.get("realtimeDepartureTime")
    if not isinstance(planned, int):
        planned = None
    if not isinstance(live, int):
        live = None
    # Without any time there is nothing to show.
    when = live if live is not None else planned
    if when is None:
        return None
    delay = entry.get("delayInMinutes")
    return {
        "time": when,
        "planned": planned if planned is not None else when,
        "delay": delay if isinstance(delay, int) else None,
        "cancelled": bool(entry.get("cancelled")),
        "realtime": bool(entry.get("realtime")) and live is not None,
        "destination": str(entry.get("destination") or ""),
    }


def select_departures(entries: list, block: Block) -> list[dict]:
    """The block's next departures, soonest first, at most two."""
    picked: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("label") or "") != block.label:
            continue
        if _direction_of(entry.get("lineId")) != block.direction:
            continue
        departure = parse_departure(entry)
        if departure is not None:
            picked.append(departure)
    picked.sort(key=lambda d: d["time"])
    return picked[:DEPARTURES_PER_BLOCK]


async def _build_board() -> dict:
    """Fetch every stop ONCE and assemble all blocks from it."""
    stop_ids = {block.stop_id for block in BLOCKS}
    async with create_mvg_client() as client:
        results = await asyncio.gather(
            *(_fetch_stop(client, stop_id) for stop_id in sorted(stop_ids)),
            return_exceptions=True,
        )
    by_stop: dict[str, list] = {}
    for stop_id, result in zip(sorted(stop_ids), results, strict=True):
        if isinstance(result, BaseException):
            raise result
        by_stop[stop_id] = result
    return {
        "groups": [
            {
                "key": block.key,
                "side": block.side,
                "label": block.label,
                "kind": block.kind,
                "stop": block.stop_name,
                "departures": select_departures(by_stop[block.stop_id], block),
            }
            for block in BLOCKS
        ]
    }


@router.get("/departures")
async def get_departures() -> dict:
    """The departure board, cached briefly and never raising past 502."""
    global _payload, _valid_until, _error, _error_valid_until
    now = _now()
    if _payload is not None and now < _valid_until:
        return _payload
    if _error is not None and now < _error_valid_until:
        raise HTTPException(status_code=502, detail=_error)
    async with _lock:
        # A second caller that queued behind the lock takes the fresh result.
        now = _now()
        if _payload is not None and now < _valid_until:
            return _payload
        if _error is not None and now < _error_valid_until:
            raise HTTPException(status_code=502, detail=_error)
        try:
            board = await _build_board()
        except TransitUnavailableError as exc:
            _error = str(exc)
            _error_valid_until = _now() + ERROR_CACHE_TTL_SECONDS
            raise HTTPException(status_code=502, detail=_error) from exc
        _payload = board
        _valid_until = _now() + DEPARTURES_CACHE_TTL_SECONDS
        _error = None
        _error_valid_until = 0.0
        return board
