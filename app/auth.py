"""Request auth: resolve the HA user behind a request and their admin status.

Header source of truth — the Supervisor ingress proxy sets exactly three
user headers on every request it proxies to the add-on (see
``_init_header`` in supervisor/api/ingress.py and the header constants in
supervisor/const.py, https://github.com/home-assistant/supervisor):

    X-Remote-User-Id            user id (always set for a session user)
    X-Remote-User-Name          username (if the user has one)
    X-Remote-User-Display-Name  display name (if the user has one)

There is deliberately NO admin flag header. Admin group membership is
therefore resolved the same way the Supervisor itself resolves users:
via the HA WebSocket command ``config/auth/list`` — reachable for this
add-on at ws://supervisor/core/websocket because of
``homeassistant_api: true`` (the Supervisor proxies the connection to
Core with its own, admin-privileged credentials). A user is an admin if
they are active and either the owner or a member of the admin group
(group id "system-admin", GROUP_ID_ADMIN in homeassistant/auth/const.py).

Trust model: the X-Remote-User-* headers are only credible because the
client IP allowlist (ClientIPAllowlistMiddleware in app.main) restricts
clients to the HA ingress proxy (172.30.32.2) and 127.0.0.1 — nobody
else can reach the app to forge them, and the ingress proxy strips any
X-Remote-User-* headers of the incoming request before setting its own
(``_init_header`` filters them).

Local development and tests run without ingress: requests from
127.0.0.1 WITHOUT the user id header count as admin (dev server, E2E
suite, container healthcheck). The real ingress proxy always sets the
header, so this fallback never applies to ingress traffic; requests
from 172.30.32.2 without an admin user are never admin (fail closed).
"""

import asyncio
import json
import logging
import os
import time

from fastapi import HTTPException, Request
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

# Header names as set by the Supervisor ingress proxy (supervisor/const.py:
# HEADER_REMOTE_USER_ID / _NAME / _DISPLAY_NAME).
REMOTE_USER_ID_HEADER = "X-Remote-User-Id"
REMOTE_USER_NAME_HEADER = "X-Remote-User-Name"
REMOTE_USER_DISPLAY_NAME_HEADER = "X-Remote-User-Display-Name"

# Admin group id in HA Core (GROUP_ID_ADMIN in homeassistant/auth/const.py).
ADMIN_GROUP_ID = "system-admin"

# Core WS API, proxied by the Supervisor for add-ons with homeassistant_api.
DEFAULT_HA_WS_URL = "ws://supervisor/core/websocket"

REQUEST_TIMEOUT_SECONDS = 5.0
# Admin group membership changes rarely; a short TTL keeps revocations
# reasonably fresh without a WS roundtrip per request.
ADMIN_CACHE_TTL_SECONDS = 60.0
# Failed lookups are cached briefly too (fail closed, but do not hammer a
# slow or down HA instance on every request).
ERROR_CACHE_TTL_SECONDS = 10.0

GERMAN_FORBIDDEN_MESSAGE = "Nur für Administratoren."


class AdminLookupError(Exception):
    """The admin user list could not be fetched from Home Assistant."""


# Cache of the admin user ids; no lock on purpose: admin checks are rare
# (page loads, admin API calls), so a concurrent miss at worst causes a
# couple of parallel WS lookups instead of stale/blocked requests.
_cached_admin_ids: frozenset[str] | None = None
_cache_valid_until = 0.0


def _now() -> float:
    """Monotonic clock; wrapped so tests can control cache expiry."""
    return time.monotonic()


def reset_cache() -> None:
    """Drop the cached admin user ids (tests)."""
    global _cached_admin_ids, _cache_valid_until
    _cached_admin_ids = None
    _cache_valid_until = 0.0


def _ws_url() -> str:
    """HA WebSocket URL (env override for local dev/tests)."""
    return os.environ.get("HA_WS_URL") or DEFAULT_HA_WS_URL


def _ws_token() -> str:
    """Token for the HA WebSocket API — same rules as app.power.create_client.

    The SUPERVISOR_TOKEN is coupled to the default supervisor URL; once
    HA_WS_URL is overridden, only an explicit HA_API_TOKEN is accepted so
    the supervisor token is never sent to a different host.
    """
    if _ws_url() == DEFAULT_HA_WS_URL:
        return os.environ.get("HA_API_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
    token = os.environ.get("HA_API_TOKEN", "")
    if not token:
        raise AdminLookupError(
            "HA_WS_URL ist gesetzt, aber HA_API_TOKEN fehlt — der"
            " SUPERVISOR_TOKEN gilt nur für die Standard-URL."
        )
    return token


def _user_is_admin(user: dict) -> bool:
    """Admin = active and (owner or member of the admin group).

    Field names follow the config/auth/list result (mirrored by the
    Supervisor's HomeAssistantUser model in supervisor/const.py).
    """
    if not user.get("is_active"):
        return False
    return bool(user.get("is_owner")) or ADMIN_GROUP_ID in (user.get("group_ids") or [])


async def _fetch_admin_user_ids() -> frozenset[str]:
    """Ids of all admin users, via the HA WS command config/auth/list.

    Speaks the standard HA WebSocket handshake (auth_required → auth →
    auth_ok) and skips unrelated messages until the command result
    arrives. Every failure mode is normalized to AdminLookupError.
    """
    token = _ws_token()
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async with connect(_ws_url()) as websocket:
                message = json.loads(await websocket.recv())
                if message.get("type") != "auth_required":
                    raise AdminLookupError(
                        f"unexpected handshake message: {message.get('type')!r}"
                    )
                await websocket.send(
                    json.dumps({"type": "auth", "access_token": token})
                )
                message = json.loads(await websocket.recv())
                if message.get("type") != "auth_ok":
                    raise AdminLookupError("WebSocket authentication rejected")
                await websocket.send(json.dumps({"id": 1, "type": "config/auth/list"}))
                while True:
                    message = json.loads(await websocket.recv())
                    if message.get("id") == 1 and message.get("type") == "result":
                        break
    except AdminLookupError:
        raise
    except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
        raise AdminLookupError(f"admin lookup failed: {exc}") from exc
    if not message.get("success"):
        raise AdminLookupError("config/auth/list was not successful")
    users = message.get("result") or []
    return frozenset(
        user["id"] for user in users if isinstance(user, dict) and _user_is_admin(user)
    )


async def is_user_admin(user_id: str) -> bool:
    """Whether the HA user id belongs to an admin; cached, fail closed."""
    global _cached_admin_ids, _cache_valid_until
    if _cached_admin_ids is None or _now() >= _cache_valid_until:
        try:
            _cached_admin_ids = await _fetch_admin_user_ids()
            _cache_valid_until = _now() + ADMIN_CACHE_TTL_SECONDS
        except AdminLookupError as exc:
            # Fail closed: an unknown admin list means no admin rights. The
            # empty result is cached briefly so a down HA instance is not
            # queried again on every single request.
            logger.warning("Admin-Lookup fehlgeschlagen (Nutzer gilt als Nicht-Admin): %s", exc)
            _cached_admin_ids = frozenset()
            _cache_valid_until = _now() + ERROR_CACHE_TTL_SECONDS
    return user_id in _cached_admin_ids


async def is_admin_request(request: Request) -> bool:
    """Admin status of the request, per the module docstring's trust model."""
    user_id = (request.headers.get(REMOTE_USER_ID_HEADER) or "").strip()
    if user_id:
        return await is_user_admin(user_id)
    # No user header: real ingress traffic always carries one, so this is
    # local traffic — the dev server, the E2E suite or the container
    # healthcheck on 127.0.0.1. Anything else (e.g. the ingress proxy
    # without a session user) is not admin.
    client = request.client
    return client is not None and client.host == "127.0.0.1"


async def require_admin(request: Request) -> None:
    """FastAPI dependency: reject non-admin requests with a German 403."""
    if not await is_admin_request(request):
        raise HTTPException(status_code=403, detail=GERMAN_FORBIDDEN_MESSAGE)
