"""FastAPI application for the Familienkalender Home Assistant add-on."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

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
                    break
        await self.app(scope, receive, send)


app = FastAPI(title="Familienkalender", docs_url=None, redoc_url=None)
app.add_middleware(IngressPathMiddleware)
# Added last so it runs first: nothing is processed for disallowed clients.
app.add_middleware(ClientIPAllowlistMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe for the add-on watchdog."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the German placeholder page (relative asset URLs, ingress-safe)."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
