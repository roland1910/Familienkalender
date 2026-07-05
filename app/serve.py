"""Process entry point: one asyncio loop serving two uvicorn listeners.

Why one process instead of two uvicorn processes started by run.sh:
run.sh can ``exec`` only one foreground process under su-exec; a second,
backgrounded uvicorn would live outside s6's signal handling and
lifetime management (risking an orphaned listener that keeps old code
running after a restart). Two programmatic ``uvicorn.Server`` instances
in a single asyncio loop give clean SIGTERM handling for both listeners
— and the feed listener can be restarted alone (certificate renewal)
without ever touching the calendar app.

Listeners:

- Main app (``app.main``) on MAIN_PORT 8099: HA ingress + healthcheck,
  plain HTTP (ingress terminates TLS at the HA frontend).
- Feed app (``app.feed_app``) on FEED_PORT 8100, mapped to host port
  8098 and forwarded by the router to the internet: TLS directly in
  uvicorn using the Let's Encrypt certificates from HA's /ssl directory
  (paths via SSL_CERTFILE/SSL_KEYFILE, see run.sh / add-on options).

The calendar must never die because of the feed: if the certificate
files are missing, the feed listener is skipped with an error in the
log while the main app runs on. A periodic check (CERT_CHECK_INTERVAL)
notices renewed certificates — uvicorn loads them only at startup, and
Let's Encrypt renews every 60-90 days — and restarts the feed listener;
the same check starts it late once missing certificates appear.
"""

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from app.feed_app import create_feed_app
from app.main import app as main_app

logger = logging.getLogger(__name__)

MAIN_PORT = 8099
FEED_PORT = 8100

# HA OS convention for Let's Encrypt certificates (map: ssl:ro in
# config.yaml); overridable via the add-on options (paths, not secrets).
DEFAULT_SSL_CERTFILE = "/ssl/fullchain.pem"
DEFAULT_SSL_KEYFILE = "/ssl/privkey.pem"

# Hourly is plenty: renewals happen every 60-90 days, and a fresh
# certificate becoming active up to an hour late is harmless.
CERT_CHECK_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class SSLPaths:
    """Certificate/key file pair for the feed listener."""

    certfile: Path
    keyfile: Path


def resolve_ssl_paths(env: Mapping[str, str] = os.environ) -> SSLPaths:
    """SSL file paths from the environment; empty values (bashio renders
    unset options as empty strings) fall back to the HA defaults."""
    return SSLPaths(
        certfile=Path(env.get("SSL_CERTFILE") or DEFAULT_SSL_CERTFILE),
        keyfile=Path(env.get("SSL_KEYFILE") or DEFAULT_SSL_KEYFILE),
    )


def cert_mtimes(paths: SSLPaths) -> tuple[float, float] | None:
    """Change fingerprint of the certificate pair, or None while either
    file is missing/unreadable."""
    try:
        return (paths.certfile.stat().st_mtime, paths.keyfile.stat().st_mtime)
    except OSError:
        return None


def decide_cert_action(
    running: bool,
    current: tuple[float, float] | None,
    new: tuple[float, float] | None,
) -> str:
    """What the periodic certificate check should do: start/restart/none.

    Pure decision logic, kept separate from the asyncio plumbing so the
    renewal behavior is unit-testable.
    """
    if not running:
        # Covers the late-provisioning case and a listener crash: certs
        # present but no listener -> (re)start.
        return "start" if new is not None else "none"
    if new is None:
        # Files vanished mid-flight (e.g. while a renewal replaces them):
        # keep serving the certificate already loaded in memory.
        return "none"
    if new != current:
        return "restart"
    return "none"


def build_main_config(*, host: str = "0.0.0.0", port: int = MAIN_PORT) -> uvicorn.Config:
    """uvicorn config for the ingress-facing calendar app."""
    # access_log=False keeps the former --no-access-log behavior (ingress
    # request noise); server_header=False avoids advertising the stack.
    return uvicorn.Config(
        main_app, host=host, port=port, access_log=False, server_header=False
    )


def build_feed_config(
    paths: SSLPaths, *, host: str = "0.0.0.0", port: int = FEED_PORT
) -> uvicorn.Config:
    """uvicorn config for the TLS feed listener."""
    return uvicorn.Config(
        create_feed_app(),
        host=host,
        port=port,
        ssl_certfile=str(paths.certfile),
        ssl_keyfile=str(paths.keyfile),
        # The auth token is part of the request URL: it must never be
        # written to an access log.
        access_log=False,
        server_header=False,
        # Clients connect directly (router port forward) — X-Forwarded-For
        # must never override the peer IP the rate limiter keys on.
        proxy_headers=False,
    )


class QuietSignalServer(uvicorn.Server):
    """uvicorn.Server that leaves signal handling to the launcher.

    With two servers in one loop, uvicorn's own capture_signals() would
    install competing process-wide handlers (each stopping only its own
    server); the launcher installs a single handler that shuts down
    everything instead (_install_signal_handlers).
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class FeedListener:
    """Owns the TLS feed server and restarts it when certificates change."""

    def __init__(
        self,
        paths: SSLPaths,
        *,
        host: str = "0.0.0.0",
        port: int = FEED_PORT,
        check_interval: float = CERT_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._paths = paths
        self._host = host
        self._port = port
        self._check_interval = check_interval
        self._server: QuietSignalServer | None = None
        self._task: asyncio.Task | None = None
        self._mtimes: tuple[float, float] | None = None
        self.restarts = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def started(self) -> bool:
        """Whether the server finished startup and accepts connections."""
        return self.running and self._server is not None and self._server.started

    @property
    def port(self) -> int | None:
        """The actually bound port (differs from the configured one for 0)."""
        if not self.started or not self._server.servers:
            return None
        return self._server.servers[0].sockets[0].getsockname()[1]

    async def run(self) -> None:
        """Serve until cancelled; re-checks the certificates periodically."""
        try:
            await self._check_certificates(initial=True)
            while True:
                await asyncio.sleep(self._check_interval)
                await self._check_certificates()
        finally:
            with suppress(asyncio.CancelledError):
                await self._stop_server()

    async def _check_certificates(self, *, initial: bool = False) -> None:
        self._reap_crashed_server()
        new = cert_mtimes(self._paths)
        action = decide_cert_action(self.running, self._mtimes, new)
        if action == "none":
            if initial and new is None:
                logger.error(
                    "Feed listener NOT started: certificate files missing "
                    "(%s / %s). The calendar keeps running; the feed comes "
                    "up automatically once the files appear.",
                    self._paths.certfile,
                    self._paths.keyfile,
                )
            return
        if action == "restart":
            logger.info("Certificate change detected — restarting the feed listener.")
            await self._stop_server()
            self.restarts += 1
        self._start_server(new)

    def _reap_crashed_server(self) -> None:
        """Log and clear a server task that ended on its own (crash)."""
        if self._task is None or not self._task.done():
            return
        exc = None if self._task.cancelled() else self._task.exception()
        if exc is not None:
            logger.error("Feed listener terminated unexpectedly: %s", exc)
        self._task = None
        self._server = None

    def _start_server(self, mtimes: tuple[float, float] | None) -> None:
        config = build_feed_config(self._paths, host=self._host, port=self._port)
        self._server = QuietSignalServer(config)
        self._task = asyncio.create_task(self._server.serve())
        self._mtimes = mtimes
        logger.info(
            "Feed listener starting on port %d (TLS, certificate %s).",
            self._port,
            self._paths.certfile,
        )

    async def _stop_server(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Feed listener shut down with an error")
        self._task = None
        self._server = None


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows dev machine: no loop signal handlers there.
            signal.signal(sig, lambda *_: stop.set())


async def serve() -> None:
    """Run both listeners until SIGTERM/SIGINT or a main-app crash."""
    main_server = QuietSignalServer(build_main_config())
    listener = FeedListener(resolve_ssl_paths())
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    main_task = asyncio.create_task(main_server.serve())
    listener_task = asyncio.create_task(listener.run())
    stop_task = asyncio.create_task(stop.wait())
    # The main app decides the process lifetime: a signal or the main
    # server ending (crash) shuts everything down — s6 then restarts the
    # add-on; the feed listener alone never keeps the process alive.
    await asyncio.wait({main_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    main_server.should_exit = True
    listener_task.cancel()
    stop_task.cancel()
    with suppress(asyncio.CancelledError):
        await listener_task
    with suppress(asyncio.CancelledError):
        await stop_task
    # Propagate a main-app crash as a nonzero exit code for the watchdog.
    await main_task


def main() -> None:
    # Root logger for the app's own messages (uvicorn configures only its
    # own loggers); INFO so the feed lifecycle is visible in the add-on log.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    asyncio.run(serve())


if __name__ == "__main__":
    main()
