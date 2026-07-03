"""FastAPI application for the Familienkalender Home Assistant add-on."""

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app import sync as sync_module
from app.filtering import DEFAULT_EVENING_BOUNDARY, filter_events
from app.models import LOCAL_TZ, StoredEvent
from app.storage import Storage, default_db_path
from app.sync import DEFAULT_SYNC_INTERVAL_SECONDS, sync_all

STATIC_DIR = Path(__file__).parent / "static"

INGRESS_HEADER = b"x-ingress-path"
INGRESS_PATH_PREFIX = "/api/hassio_ingress/"

# HA ingress proxy plus localhost for the container-internal healthcheck.
DEFAULT_ALLOWED_CLIENT_IPS = "172.30.32.2,127.0.0.1"


def _allowed_client_ips() -> frozenset[str]:
    """Read the client IP allowlist from the environment (tests/local dev)."""
    raw = os.environ.get("ALLOWED_CLIENT_IPS", DEFAULT_ALLOWED_CLIENT_IPS)
    return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())


def _is_valid_ingress_path(value: str) -> bool:
    """Accept only plausible HA ingress base paths.

    The value becomes the ASGI root_path and thus ends up in generated URLs,
    so reject anything that does not look like /api/hassio_ingress/<token>:
    no scheme separators, no whitespace, no control characters.
    """
    if not value.startswith(INGRESS_PATH_PREFIX):
        return False
    if "://" in value:
        return False
    # ord < 33 covers space and ASCII control characters, 127 is DEL.
    return all(32 < ord(char) != 127 for char in value)


class ClientIPAllowlistMiddleware:
    """Reject requests whose client IP is not on the allowlist.

    The add-on must only be reachable through the HA ingress proxy
    (172.30.32.2); ingress itself handles HA authentication. Everything
    else (e.g. direct access to the container port) is answered with 403.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.allowed_ips = _allowed_client_ips()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            client_host = client[0] if client else None
            if client_host not in self.allowed_ips:
                response = PlainTextResponse("Forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class IngressPathMiddleware:
    """Expose the HA ingress base path as the ASGI root_path.

    Home Assistant ingress strips its prefix (/api/hassio_ingress/<token>)
    before proxying and sends it in the X-Ingress-Path header. Setting it as
    root_path keeps generated URLs (url_for, redirects, docs) correct.
    Invalid header values are ignored, never an error.

    The ASGI spec requires "path" to contain root_path as a prefix; Starlette
    relies on that and strips root_path from the path when routing
    (get_route_path). Because the proxy already stripped the prefix, it has
    to be re-added here — otherwise mounted apps (StaticFiles) resolve the
    wrong path and every asset 404s behind ingress.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == INGRESS_HEADER:
                    ingress_path = value.decode("latin-1")
                    if _is_valid_ingress_path(ingress_path):
                        scope["root_path"] = ingress_path
                        path = scope.get("path", "")
                        if not path.startswith(ingress_path):
                            scope["path"] = ingress_path + path
                        raw_path = scope.get("raw_path")
                        ingress_raw = ingress_path.encode("latin-1")
                        if raw_path is not None and not raw_path.startswith(ingress_raw):
                            scope["raw_path"] = ingress_raw + raw_path
                    break
        await self.app(scope, receive, send)


# Unbounded cache (equivalent to lru_cache(maxsize=None)): there is
# exactly one DATA_DIR in production and one per test; a bounded cache
# could evict (and later recreate) a Storage that is still in use, which
# buys nothing and costs re-initialization.
@cache
def _storage_for(db_path: Path) -> Storage:
    return Storage(db_path)


def get_storage() -> Storage:
    """Storage for the current DATA_DIR (env is re-read so tests can vary it)."""
    return _storage_for(default_db_path())


def _evening_boundary() -> time:
    """Evening boundary for the family filter (env EVENING_BOUNDARY, HH:MM).

    Becomes an admin UI setting later; until then the env var keeps it
    configurable without a rebuild.
    """
    raw = os.environ.get("EVENING_BOUNDARY", "")
    if raw:
        try:
            return time.fromisoformat(raw)
        except ValueError:
            pass
    return DEFAULT_EVENING_BOUNDARY


def _sync_interval_seconds() -> float:
    raw = os.environ.get("SYNC_INTERVAL_SECONDS", "")
    try:
        return float(raw) if raw else DEFAULT_SYNC_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_SYNC_INTERVAL_SECONDS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the periodic sync as a background task while the app is up."""
    interval = _sync_interval_seconds()
    task: asyncio.Task | None = None
    if interval > 0:
        task = asyncio.create_task(sync_module.periodic_sync(get_storage(), interval))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Familienkalender", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(IngressPathMiddleware)
# Added last so it runs first: nothing is processed for disallowed clients.
app.add_middleware(ClientIPAllowlistMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe for the add-on watchdog."""
    return {"status": "ok"}


def _serialize_event(item: StoredEvent) -> dict:
    """JSON shape for the frontend; timed events in local time, all-day as dates.

    All-day ``end`` keeps the iCalendar semantics (exclusive end date).
    """
    event = item.event
    if event.all_day:
        start = event.start.isoformat()
        end = event.end.isoformat()
    else:
        start = event.start_as_datetime().astimezone(LOCAL_TZ).isoformat()
        end = event.end_as_datetime().astimezone(LOCAL_TZ).isoformat()
    return {
        "source_id": item.source_id,
        "source_name": item.source_name,
        "uid": event.uid,
        "title": event.title,
        "start": start,
        "end": end,
        "all_day": event.all_day,
        "location": event.location,
    }


@app.get("/api/events")
async def list_events(
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> dict:
    """Aggregated events for [from, to] (inclusive local calendar days).

    Sources with display_mode=filtered only contribute family-relevant
    events (see app.filtering).
    """
    if from_date > to_date:
        raise HTTPException(status_code=400, detail="'from' muss vor 'to' liegen")
    range_start = datetime.combine(from_date, time.min, tzinfo=LOCAL_TZ)
    range_end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    boundary = _evening_boundary()
    # filter_events is the single place deciding what display_mode means;
    # it is called per event because each StoredEvent carries its own mode.
    events = [
        _serialize_event(item)
        for item in get_storage().get_events(range_start, range_end)
        if filter_events([item.event], display_mode=item.display_mode, boundary=boundary)
    ]
    return {"events": events}


@app.get("/api/sources")
async def list_sources() -> dict:
    """Configured sources with sync status — deliberately without config/secrets."""
    return {
        "sources": [
            {
                "id": source.id,
                "type": source.type,
                "name": source.name,
                "enabled": source.enabled,
                "display_mode": source.display_mode,
                "last_sync_at": (
                    source.last_sync_at.isoformat() if source.last_sync_at else None
                ),
                "last_sync_error": source.last_sync_error,
            }
            for source in get_storage().list_sources()
        ]
    }


@app.post("/api/sync")
async def trigger_sync() -> dict:
    """Manually trigger a sync of all enabled sources.

    While a sync is already running (periodic or manual) the request is
    rejected instead of queueing a redundant second run behind the lock.
    """
    if sync_module.SYNC_LOCK.locked():
        raise HTTPException(
            status_code=409, detail="Eine Synchronisierung läuft bereits."
        )
    results = await sync_all(get_storage())
    return {"results": {str(source_id): error for source_id, error in results.items()}}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the German placeholder page (relative asset URLs, ingress-safe)."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
