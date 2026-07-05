"""FastAPI application for the Familienkalender Home Assistant add-on."""

import asyncio
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from app import sync as sync_module
from app.admin import router as admin_router
from app.auth import is_admin_request
from app.filtering import filter_events
from app.models import LOCAL_TZ, StoredEvent
from app.power import router as power_router
from app.settings import get_evening_boundary
from app.slideshow import admin_router as slideshow_admin_router
from app.slideshow import periodic_photo_scan
from app.slideshow import router as slideshow_router
from app.storage import get_storage
from app.sync import DEFAULT_SYNC_INTERVAL_SECONDS, sync_all
from app.tags import router as tags_router

STATIC_DIR = Path(__file__).parent / "static"

INGRESS_HEADER = b"x-ingress-path"
# HA ingress base paths are /api/hassio_ingress/<token>; tokens are URL-safe
# base64-ish. Everything else (traversal, markup, extra segments) is rejected.
INGRESS_PATH_PATTERN = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+$")

# HA ingress proxy plus localhost for the container-internal healthcheck.
DEFAULT_ALLOWED_CLIENT_IPS = "172.30.32.2,127.0.0.1"

# Global cap for request bodies: no endpoint of this app takes payloads
# anywhere near this size (the largest are small JSON settings updates).
MAX_REQUEST_BODY_BYTES = 16 * 1024


def _allowed_client_ips() -> frozenset[str]:
    """Read the client IP allowlist from the environment (tests/local dev)."""
    raw = os.environ.get("ALLOWED_CLIENT_IPS", DEFAULT_ALLOWED_CLIENT_IPS)
    return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())


def _is_valid_ingress_path(value: str) -> bool:
    """Accept only plausible HA ingress base paths.

    The value becomes the ASGI root_path and thus ends up in generated URLs,
    so accept strictly /api/hassio_ingress/<token> with a URL-safe token —
    no traversal, no whitespace or control characters, no markup, no
    additional path segments.
    """
    return INGRESS_PATH_PATTERN.fullmatch(value) is not None


class ClientIPAllowlistMiddleware:
    """Reject requests whose client IP is not on the allowlist.

    The main app must only be reachable through the HA ingress proxy
    (172.30.32.2); ingress itself handles HA authentication. Everything
    else (e.g. direct access to the container port) is answered with 403
    — without exceptions: the ICS feed, which needs to be reachable past
    ingress, runs as its own app tree on a separate port (app.feed_app,
    started by app.serve), so no allowlist hole exists here anymore.
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


class RequestBodyLimitMiddleware:
    """Reject request bodies above MAX_REQUEST_BODY_BYTES with 413.

    Declared bodies (Content-Length) are rejected up front. Bodies without
    a declared length (chunked) are cut off once the limit is exceeded:
    the truncated payload then fails JSON parsing in the endpoint (422)
    instead of ever being buffered in full. Both paths keep oversized
    payloads away from every POST/PUT endpoint centrally, rather than
    relying on per-endpoint field caps alone.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                if declared is not None and declared > MAX_REQUEST_BODY_BYTES:
                    response = JSONResponse({"detail": "Anfrage zu groß."}, status_code=413)
                    await response(scope, receive, send)
                    return
                break

        received = 0

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BODY_BYTES:
                    # Stop feeding the app; the truncated body fails parsing.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        await self.app(scope, limited_receive, send)


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
                            # raw_path mirrors path and must stay consistent
                            # with it, so it is prefixed exactly when path
                            # was prefixed — never based on its own content.
                            raw_path = scope.get("raw_path")
                            if raw_path is not None:
                                scope["raw_path"] = ingress_path.encode("latin-1") + raw_path
                    break
        await self.app(scope, receive, send)


def _sync_interval_seconds() -> float:
    raw = os.environ.get("SYNC_INTERVAL_SECONDS", "")
    try:
        return float(raw) if raw else DEFAULT_SYNC_INTERVAL_SECONDS
    except ValueError:
        return DEFAULT_SYNC_INTERVAL_SECONDS


def _photo_scan_enabled() -> bool:
    """Whether the periodic photo scan runs (disabled for tests/local dev).

    Off by default unless SLIDESHOW_SCAN=1, so the test/E2E servers and a
    local dev machine without a real /media share never kick off a scan.
    """
    return os.environ.get("SLIDESHOW_SCAN") == "1"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the periodic sync and photo scan as background tasks while up."""
    interval = _sync_interval_seconds()
    tasks: list[asyncio.Task] = []
    if interval > 0:
        tasks.append(
            asyncio.create_task(sync_module.periodic_sync(get_storage(), interval))
        )
    if _photo_scan_enabled():
        tasks.append(asyncio.create_task(periodic_photo_scan()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Familienkalender", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(admin_router)
app.include_router(tags_router)
app.include_router(power_router)
app.include_router(slideshow_router)
app.include_router(slideshow_admin_router)
# Innermost (added first): runs after allowlist and ingress handling.
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(IngressPathMiddleware)
# Added last so it runs first: nothing is processed for disallowed clients.
app.add_middleware(ClientIPAllowlistMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe for the add-on watchdog."""
    return {"status": "ok"}


@app.get("/api/me")
async def me(request: Request) -> dict[str, bool]:
    """Role of the requesting HA user — the frontend hides the admin gear
    for non-admins (the server-side gate is require_admin, not this)."""
    return {"is_admin": await is_admin_request(request)}


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
        # Admin-configured source color ("#rrggbb", validated on write) or
        # "" for the frontend's palette default.
        "source_color": item.color,
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
    boundary = get_evening_boundary(get_storage())
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
                "color": source.color,
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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    """Serve the admin page (same relative-URL rules as the calendar).

    Non-admins get a proper German 403 page instead of raw JSON — the
    gear is hidden for them, but the URL is guessable.
    """
    if not await is_admin_request(request):
        return HTMLResponse(
            (STATIC_DIR / "admin" / "forbidden.html").read_text(encoding="utf-8"),
            status_code=403,
        )
    return HTMLResponse((STATIC_DIR / "admin" / "admin.html").read_text(encoding="utf-8"))
