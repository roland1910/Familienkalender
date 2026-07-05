"""Process entry point: one asyncio loop serving two uvicorn listeners.

Why one process instead of two uvicorn processes started by run.sh:
run.sh can supervise only one foreground process under su-exec; a
second, backgrounded uvicorn would live outside s6's signal handling
and lifetime management (risking an orphaned listener that keeps old
code running after a restart). Two programmatic ``uvicorn.Server``
instances in a single asyncio loop give clean SIGTERM handling for both
listeners.

Listeners:

- Main app (``app.main``) on MAIN_PORT 8099: HA ingress + healthcheck,
  plain HTTP (ingress terminates TLS at the HA frontend).
- Feed app (``app.feed_app``) on FEED_PORT 8100, mapped to host port
  8098 and forwarded by the router to the internet: TLS directly in
  uvicorn.

Certificate staging (shared contract with run.sh): the Let's Encrypt
key in HA's /ssl is root-only (0600) while this process runs as the
unprivileged ``app`` user — it can never read the original. run.sh
therefore stages app-readable copies into a tmpfs directory as root
before dropping privileges and exports two path pairs:

- ``SSL_CERTFILE``/``SSL_KEYFILE``: the staged copies uvicorn loads.
- ``SSL_SOURCE_CERTFILE``/``SSL_SOURCE_KEYFILE``: the originals, only
  ever ``stat``-ed here (stat needs no read permission) to notice
  renewals. In local development without run.sh they are unset and the
  watch falls back to the load paths.

uvicorn loads certificates only at startup and Let's Encrypt renews
every 60-90 days, so a periodic check (CERT_CHECK_INTERVAL) watches the
source mtimes. On ANY observable source change — a renewal as well as
certificates appearing for the first time — the process shuts down both
listeners in an orderly fashion and exits with CERT_RELOAD_EXIT_CODE:
only run.sh (root) can refresh the staged copies, then it restarts us.

The calendar must never die because of the feed: if the staged copies
are missing or unreadable, the feed listener is skipped with an error
in the log while the main app runs on.
"""

import asyncio
import contextlib
import logging
import os
import signal
import sys
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

# Process exit code meaning "certificate sources changed, restage and
# restart me". run.sh's supervisor loop hardcodes the same value: it
# re-copies the certificates (as root) and starts the process again;
# every other exit code is passed through and ends the add-on (s6).
CERT_RELOAD_EXIT_CODE = 86

# Hourly is plenty: renewals happen every 60-90 days, and a fresh
# certificate becoming active up to an hour late is harmless.
CERT_CHECK_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class SSLPaths:
    """Certificate/key file pair for the feed listener."""

    certfile: Path
    keyfile: Path


def resolve_ssl_paths(env: Mapping[str, str] = os.environ) -> SSLPaths:
    """SSL file paths uvicorn loads (the staged, app-readable copies);
    empty values (bashio renders unset options as empty strings) fall
    back to the HA defaults."""
    return SSLPaths(
        certfile=Path(env.get("SSL_CERTFILE") or DEFAULT_SSL_CERTFILE),
        keyfile=Path(env.get("SSL_KEYFILE") or DEFAULT_SSL_KEYFILE),
    )


def resolve_ssl_source_paths(env: Mapping[str, str] = os.environ) -> SSLPaths:
    """SSL file paths whose mtimes are watched for renewals (the
    root-only originals under /ssl, exported by run.sh). Without the
    SOURCE variables — local development without run.sh — the watch
    falls back to the load paths."""
    load = resolve_ssl_paths(env)
    return SSLPaths(
        certfile=Path(env.get("SSL_SOURCE_CERTFILE") or load.certfile),
        keyfile=Path(env.get("SSL_SOURCE_KEYFILE") or load.keyfile),
    )


def cert_mtimes(paths: SSLPaths) -> tuple[float, float] | None:
    """Change fingerprint of the certificate pair, or None while either
    file is missing. stat works without read permission, so this is
    safe on the root-only source files."""
    try:
        return (paths.certfile.stat().st_mtime, paths.keyfile.stat().st_mtime)
    except OSError:
        return None


def certs_readable(paths: SSLPaths) -> bool:
    """Whether this process can actually open both staged copies —
    unlike stat, this is what uvicorn's load_cert_chain needs."""
    return os.access(paths.certfile, os.R_OK) and os.access(paths.keyfile, os.R_OK)


def decide_cert_action(
    running: bool,
    current: tuple[float, float] | None,
    new: tuple[float, float] | None,
) -> str:
    """What the periodic certificate check should do: exit/start/none.

    ``current``/``new`` are source-mtime fingerprints. Any observable
    source change — a renewal as well as certificates appearing for the
    first time — yields "exit": the fresh files need restaging by root
    (run.sh), which this unprivileged process cannot do itself, so it
    exits with CERT_RELOAD_EXIT_CODE. "start" only recovers a crashed
    listener while the sources (and thus the staged copies) are
    unchanged. Pure decision logic, kept separate from the asyncio
    plumbing so the renewal behavior is unit-testable.
    """
    if new is not None and new != current:
        return "exit"
    if not running and new is not None:
        # Listener down but sources unchanged (crash): the staged copies
        # are still valid, recover in-process.
        return "start"
    # Sources vanished mid-flight (e.g. while a renewal replaces them):
    # keep serving the staged copies that are already loaded.
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


def _log_server_crash(task: asyncio.Task) -> None:
    """Done callback for the feed server task: log a crash immediately.

    Orderly endings (cancellation, clean shutdown) stay quiet; retrieving
    the exception here also silences asyncio's "exception was never
    retrieved" noise for tasks that die between certificate checks.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Feed listener terminated unexpectedly: %r", exc)


class FeedListener:
    """Owns the TLS feed server and watches the certificate sources.

    ``run()`` serves until cancelled — or returns cleanly when the
    source certificates changed, which the caller must translate into a
    process exit with CERT_RELOAD_EXIT_CODE (see module docstring).
    """

    def __init__(
        self,
        paths: SSLPaths,
        *,
        source_paths: SSLPaths | None = None,
        host: str = "0.0.0.0",
        port: int = FEED_PORT,
        check_interval: float = CERT_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._paths = paths
        self._source_paths = source_paths if source_paths is not None else paths
        self._host = host
        self._port = port
        self._check_interval = check_interval
        self._server: QuietSignalServer | None = None
        self._task: asyncio.Task | None = None
        self._mtimes: tuple[float, float] | None = None

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
        """Serve until cancelled or until the certificate sources change
        (clean return — the caller exits with CERT_RELOAD_EXIT_CODE)."""
        try:
            self._initial_check()
            while True:
                await asyncio.sleep(self._check_interval)
                if self._check_certificates():
                    logger.info(
                        "Certificate change detected (%s) — shutting down for "
                        "restaging and restart (exit code %d).",
                        self._source_paths.certfile,
                        CERT_RELOAD_EXIT_CODE,
                    )
                    return
        finally:
            with suppress(asyncio.CancelledError):
                await self._stop_server()

    def _initial_check(self) -> None:
        """Record the source fingerprint and start the listener if the
        staged copies are usable; otherwise leave the feed off (the
        calendar must never die because of the feed)."""
        self._mtimes = cert_mtimes(self._source_paths)
        if self._mtimes is None:
            logger.error(
                "Feed listener NOT started: certificate files missing "
                "(%s / %s). The calendar keeps running; the feed comes "
                "up automatically once the files appear.",
                self._source_paths.certfile,
                self._source_paths.keyfile,
            )
            return
        if not certs_readable(self._paths):
            logger.error(
                "Feed listener NOT started: staged certificate copies not "
                "readable (%s / %s). The calendar keeps running; check the "
                "staging step in run.sh.",
                self._paths.certfile,
                self._paths.keyfile,
            )
            return
        self._start_server()

    def _check_certificates(self) -> bool:
        """One periodic check; True means the sources changed and the
        process must exit for restaging."""
        self._reap_crashed_server()
        new = cert_mtimes(self._source_paths)
        action = decide_cert_action(self.running, self._mtimes, new)
        if action == "exit":
            return True
        if action == "start" and certs_readable(self._paths):
            self._start_server()
        return False

    def _reap_crashed_server(self) -> None:
        """Clear a server task that ended on its own; the crash itself
        was already logged immediately by the task's done callback."""
        if self._task is None or not self._task.done():
            return
        self._task = None
        self._server = None

    def _start_server(self) -> None:
        config = build_feed_config(self._paths, host=self._host, port=self._port)
        self._server = QuietSignalServer(config)
        self._task = asyncio.create_task(self._server.serve())
        # Surface a crash in the log the moment it happens — the periodic
        # certificate check would notice the dead task only up to an hour
        # later, leaving the feed silently down in between.
        self._task.add_done_callback(_log_server_crash)
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
                pass  # already logged by the task's done callback
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


async def serve(
    *,
    feed_host: str = "0.0.0.0",
    feed_port: int = FEED_PORT,
    feed_check_interval: float = CERT_CHECK_INTERVAL_SECONDS,
) -> int:
    """Run both listeners; returns the process exit code.

    0 after SIGTERM/SIGINT, CERT_RELOAD_EXIT_CODE when the feed listener
    detected changed certificate sources (run.sh restages the copies and
    restarts the process); a main-app crash propagates as an exception
    (nonzero exit for the s6 watchdog). The keyword arguments exist for
    tests (ephemeral ports, fast certificate checks).
    """
    main_server = QuietSignalServer(build_main_config())
    listener = FeedListener(
        resolve_ssl_paths(),
        source_paths=resolve_ssl_source_paths(),
        host=feed_host,
        port=feed_port,
        check_interval=feed_check_interval,
    )
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    main_task = asyncio.create_task(main_server.serve())
    listener_task = asyncio.create_task(listener.run())
    stop_task = asyncio.create_task(stop.wait())

    exit_code = 0
    # The main app decides the process lifetime: a signal or the main
    # server ending (crash) shuts everything down — s6 then restarts the
    # add-on. The listener task ending cleanly means "certificate sources
    # changed": shut everything down too, but exit with the reload code.
    await asyncio.wait(
        {main_task, listener_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if (
        listener_task.done()
        and not listener_task.cancelled()
        and not main_task.done()
        and not stop.is_set()
    ):
        exc = listener_task.exception()
        if exc is None:
            exit_code = CERT_RELOAD_EXIT_CODE
        else:
            # The feed must never take the calendar down: log the crash
            # and keep the main app running (the feed stays off until the
            # next add-on restart).
            logger.error(
                "Feed listener supervisor failed: %r — the calendar keeps running.",
                exc,
            )
            await asyncio.wait(
                {main_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )

    main_server.should_exit = True
    listener_task.cancel()
    stop_task.cancel()
    with suppress(asyncio.CancelledError):
        await listener_task
    with suppress(asyncio.CancelledError):
        await stop_task
    # Propagate a main-app crash as a nonzero exit code for the watchdog.
    await main_task
    return exit_code


def main() -> None:
    # Root logger for the app's own messages (uvicorn configures only its
    # own loggers); INFO so the feed lifecycle is visible in the add-on log.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    exit_code = asyncio.run(serve())
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
