"""Standalone ASGI app for the ICS feed listener.

The feed is deliberately reachable from the internet (router forwards
external port 8098 to this listener, container port 8100 — see
config.yaml and app.serve), so this app tree contains ONLY the feed
route: even if a routing bug slipped in, nothing else is bound on this
port. TLS termination happens in uvicorn (app.serve); the URL token
remains the sole authentication of the route itself.

Hardening on top of the token (FeedHardeningMiddleware):

- only GET/HEAD are accepted (405 otherwise), no docs/openapi routes
- security headers on every response (HSTS, nosniff, referrer policy,
  private caching)
- in-memory rate limiting per client IP plus a global backstop
- lockout after repeated wrong-token attempts; the lockout is absolute,
  i.e. a request with a valid token is rejected too while it lasts, so
  a brute-force run cannot confirm a hit

Log lines mention client IPs and counters, never the token (the URL is
also kept out of logs via access_log=False in app.serve).
"""

import logging
import math
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.feed import build_feed
from app.settings import get_feed_token
from app.storage import get_storage

logger = logging.getLogger(__name__)

# -- rate limiting constants -------------------------------------------------

# Sliding window for both the per-IP and the global request counters.
RATE_WINDOW_SECONDS = 60
# Per client IP: generous for calendar clients (the suggested refresh
# interval is 15 minutes), far too little for brute-forcing a 256-bit token.
MAX_REQUESTS_PER_IP_PER_MINUTE = 30
# Backstop across all clients: the family has a handful of subscribers;
# this only trips when something is systematically wrong (e.g. distributed
# scraping) and protects the Pi from being busied with feed builds.
GLOBAL_MAX_REQUESTS_PER_MINUTE = 300
# After this many wrong-token responses from one IP within LOCKOUT_SECONDS
# the IP is locked out entirely for LOCKOUT_SECONDS (measured from the
# failure that has to age out) — even requests with a valid token get 429.
FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT = 10
LOCKOUT_SECONDS = 15 * 60
# Upper bound for tracked client IPs (memory DoS guard): the least
# recently seen entries are evicted first. Source addresses cannot be
# spoofed across a TCP+TLS handshake, so flushing the table to reset a
# lockout would take this many real, distinct clients.
MAX_TRACKED_IPS = 1024

ALLOWED_METHODS = ("GET", "HEAD")

# Added to every response (including 404/405/429). Cache-Control keeps the
# calendar payload out of shared caches; HSTS pins clients to https on the
# externally forwarded port.
_SECURITY_HEADERS = (
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"cache-control", b"private, max-age=300"),
    (b"referrer-policy", b"no-referrer"),
)


@dataclass
class _IPEntry:
    """Per-IP bookkeeping: request timestamps and failed token attempts."""

    requests: deque[float] = field(default_factory=deque)
    failures: deque[float] = field(default_factory=deque)


class RateLimiter:
    """In-memory request limiter for the feed listener.

    Deliberately not thread-safe: the feed app runs in a single asyncio
    event loop and every check completes synchronously within one
    request. The clock is injectable for tests (monotonic seconds).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: OrderedDict[str, _IPEntry] = OrderedDict()
        self._global_requests: deque[float] = deque()

    @property
    def tracked_ip_count(self) -> int:
        return len(self._entries)

    def check(self, ip: str) -> int | None:
        """Admission check for one request: None = allowed, otherwise the
        number of seconds after which a retry could succeed."""
        now = self._clock()
        self._purge(self._global_requests, now - RATE_WINDOW_SECONDS)
        if len(self._global_requests) >= GLOBAL_MAX_REQUESTS_PER_MINUTE:
            retry = self._retry_after(self._global_requests[0] + RATE_WINDOW_SECONDS, now)
            logger.warning(
                "Feed: global rate limit reached (%d/min), rejecting %s for %ss",
                GLOBAL_MAX_REQUESTS_PER_MINUTE,
                ip,
                retry,
            )
            return retry
        entry = self._entry(ip, now)
        if len(entry.failures) >= FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT:
            # Locked until enough failures age out of the lockout window;
            # the one that has to expire next is at index len - threshold.
            unlock_at = (
                entry.failures[len(entry.failures) - FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT]
                + LOCKOUT_SECONDS
            )
            retry = self._retry_after(unlock_at, now)
            logger.warning(
                "Feed: %s is locked out (%d failed token attempts), retry in %ss",
                ip,
                len(entry.failures),
                retry,
            )
            return retry
        if len(entry.requests) >= MAX_REQUESTS_PER_IP_PER_MINUTE:
            retry = self._retry_after(entry.requests[0] + RATE_WINDOW_SECONDS, now)
            logger.warning(
                "Feed: rate limit for %s reached (%d/min), retry in %ss",
                ip,
                MAX_REQUESTS_PER_IP_PER_MINUTE,
                retry,
            )
            return retry
        entry.requests.append(now)
        self._global_requests.append(now)
        return None

    def register_failed_token(self, ip: str) -> None:
        """Record a wrong-token response (the route's 404) for this IP."""
        now = self._clock()
        entry = self._entry(ip, now)
        entry.failures.append(now)
        count = len(entry.failures)
        if count >= FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT:
            logger.warning(
                "Feed: locking out %s for %ds after %d failed token attempts",
                ip,
                LOCKOUT_SECONDS,
                count,
            )
        else:
            logger.info(
                "Feed: failed token attempt from %s (%d/%d)",
                ip,
                count,
                FAILED_TOKEN_ATTEMPTS_BEFORE_LOCKOUT,
            )

    def _entry(self, ip: str, now: float) -> _IPEntry:
        """The (purged) entry for this IP, tracked LRU with a size bound."""
        entry = self._entries.get(ip)
        if entry is None:
            entry = _IPEntry()
            self._entries[ip] = entry
        else:
            self._entries.move_to_end(ip)
        self._purge(entry.requests, now - RATE_WINDOW_SECONDS)
        self._purge(entry.failures, now - LOCKOUT_SECONDS)
        while len(self._entries) > MAX_TRACKED_IPS:
            self._entries.popitem(last=False)
        return entry

    @staticmethod
    def _purge(timestamps: deque[float], cutoff: float) -> None:
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    @staticmethod
    def _retry_after(when: float, now: float) -> int:
        return max(1, math.ceil(when - now))


class FeedHardeningMiddleware:
    """Method restriction, rate limiting and security headers.

    Runs outside the router, so the limits also cover probes against
    non-existent paths and the security headers reach every response.
    """

    def __init__(self, app: ASGIApp, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(h for h in _SECURITY_HEADERS if h[0] not in present)
                message = {**message, "headers": headers}
            await send(message)

        if scope.get("method", "").upper() not in ALLOWED_METHODS:
            response = PlainTextResponse(
                "Methode nicht erlaubt.",
                status_code=405,
                headers={"Allow": ", ".join(ALLOWED_METHODS)},
            )
            await response(scope, receive, send_with_headers)
            return

        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        retry_after = self.limiter.check(client_ip)
        if retry_after is not None:
            response = PlainTextResponse(
                "Zu viele Anfragen.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send_with_headers)
            return

        await self.app(scope, receive, send_with_headers)


def create_feed_app(*, clock: Callable[[], float] = time.monotonic) -> FastAPI:
    """The feed-only ASGI app; the clock is injectable for rate-limit tests."""
    app = FastAPI(
        title="Familienkalender Feed",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    limiter = RateLimiter(clock=clock)
    app.state.rate_limiter = limiter

    # Explicit HEAD support: subscription clients probe with HEAD before
    # downloading; FastAPI does not add HEAD to GET routes by itself.
    @app.api_route("/feed/{token}.ics", methods=["GET", "HEAD"])
    async def feed(token: str, request: Request) -> Response:
        """Subscribable ICS feed — the URL token is the sole authentication.

        Missing token setup or a wrong token both answer 404 so probing
        reveals nothing about the feed's existence; either case counts as
        a failed attempt towards the lockout. Non-ASCII input is rejected
        before the comparison: secrets.compare_digest only accepts ASCII
        str and raises TypeError otherwise, which would otherwise surface
        as an unhandled 500.
        """
        stored = get_feed_token(get_storage())
        if not stored or not token.isascii() or not secrets.compare_digest(token, stored):
            limiter.register_failed_token(request.client.host if request.client else "unknown")
            raise HTTPException(status_code=404, detail="Nicht gefunden.")
        return Response(
            content=build_feed(get_storage()),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="familie-roland.ics"'},
        )

    app.add_middleware(FeedHardeningMiddleware, limiter=limiter)
    return app
