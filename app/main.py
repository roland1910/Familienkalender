"""FastAPI application for the Familienkalender Home Assistant add-on."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

STATIC_DIR = Path(__file__).parent / "static"

INGRESS_HEADER = b"x-ingress-path"


class IngressPathMiddleware:
    """Expose the HA ingress base path as the ASGI root_path.

    Home Assistant ingress strips its prefix (/api/hassio_ingress/<token>)
    before proxying and sends it in the X-Ingress-Path header. Setting it as
    root_path keeps generated URLs (url_for, redirects, docs) correct.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == INGRESS_HEADER:
                    scope["root_path"] = value.decode("latin-1")
                    break
        await self.app(scope, receive, send)


app = FastAPI(title="Familienkalender", docs_url=None, redoc_url=None)
app.add_middleware(IngressPathMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness probe for the add-on watchdog."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the German placeholder page (relative asset URLs, ingress-safe)."""
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
