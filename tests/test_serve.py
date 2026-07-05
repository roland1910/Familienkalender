"""Tests for the launcher (app.serve): two uvicorn listeners, one process.

The pure decision logic (SSL path resolution, certificate mtime
fingerprint, exit/start decision, uvicorn configs) is unit-tested
everywhere. The real TLS handshake and the exit-on-renewal behavior run
against a live listener with an on-the-fly self-signed certificate;
those tests need the openssl CLI (present in Git Bash on the dev
machine) and are skipped without it — the final TLS verification point
is the Pi deployment either way.

Certificate staging model (mirrors run.sh): the originals in /ssl are
root-only, so run.sh copies them to an app-readable staging directory
before dropping privileges and exports SSL_CERTFILE/SSL_KEYFILE (the
copies, loaded by uvicorn) plus SSL_SOURCE_CERTFILE/SSL_SOURCE_KEYFILE
(the originals, watched via stat — stat needs no read permission).
When the sources change, the process exits with CERT_RELOAD_EXIT_CODE
and run.sh stages fresh copies and restarts it.
"""

import asyncio
import os
import shutil
import subprocess
import time
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

import app.serve as serve_module
from app.serve import (
    CERT_RELOAD_EXIT_CODE,
    DEFAULT_SSL_CERTFILE,
    DEFAULT_SSL_KEYFILE,
    FEED_PORT,
    MAIN_PORT,
    FeedListener,
    SSLPaths,
    build_feed_config,
    build_main_config,
    cert_mtimes,
    decide_cert_action,
    resolve_ssl_paths,
    resolve_ssl_source_paths,
    serve,
)
from app.settings import ensure_feed_token
from app.storage import Storage, default_db_path

pytestmark = pytest.mark.anyio


class TestResolveSSLPaths:
    def test_defaults_point_to_the_ha_ssl_directory(self) -> None:
        paths = resolve_ssl_paths(env={})
        assert paths.certfile == Path(DEFAULT_SSL_CERTFILE)
        assert paths.keyfile == Path(DEFAULT_SSL_KEYFILE)

    def test_environment_overrides_both_paths(self) -> None:
        paths = resolve_ssl_paths(
            env={"SSL_CERTFILE": "/ssl/mein-cert.pem", "SSL_KEYFILE": "/ssl/mein-key.pem"}
        )
        assert paths.certfile == Path("/ssl/mein-cert.pem")
        assert paths.keyfile == Path("/ssl/mein-key.pem")

    def test_empty_values_fall_back_to_the_defaults(self) -> None:
        # bashio::config renders unset options as empty strings.
        paths = resolve_ssl_paths(env={"SSL_CERTFILE": "", "SSL_KEYFILE": ""})
        assert paths.certfile == Path(DEFAULT_SSL_CERTFILE)
        assert paths.keyfile == Path(DEFAULT_SSL_KEYFILE)


class TestResolveSSLSourcePaths:
    def test_source_variables_win(self) -> None:
        # run.sh: watch the root-only originals, load the staged copies.
        paths = resolve_ssl_source_paths(
            env={
                "SSL_CERTFILE": "/run/familienkalender-ssl/fullchain.pem",
                "SSL_KEYFILE": "/run/familienkalender-ssl/privkey.pem",
                "SSL_SOURCE_CERTFILE": "/ssl/fullchain.pem",
                "SSL_SOURCE_KEYFILE": "/ssl/privkey.pem",
            }
        )
        assert paths.certfile == Path("/ssl/fullchain.pem")
        assert paths.keyfile == Path("/ssl/privkey.pem")

    def test_without_source_variables_falls_back_to_the_load_paths(self) -> None:
        # Local development without run.sh: watch what is loaded.
        paths = resolve_ssl_source_paths(
            env={"SSL_CERTFILE": "/tmp/c.pem", "SSL_KEYFILE": "/tmp/k.pem"}
        )
        assert paths.certfile == Path("/tmp/c.pem")
        assert paths.keyfile == Path("/tmp/k.pem")

    def test_empty_environment_yields_the_ha_defaults(self) -> None:
        paths = resolve_ssl_source_paths(env={})
        assert paths.certfile == Path(DEFAULT_SSL_CERTFILE)
        assert paths.keyfile == Path(DEFAULT_SSL_KEYFILE)


class TestCertMtimes:
    def test_both_files_present_yields_a_fingerprint(self, tmp_path: Path) -> None:
        cert = tmp_path / "fullchain.pem"
        key = tmp_path / "privkey.pem"
        cert.write_text("cert")
        key.write_text("key")
        fingerprint = cert_mtimes(SSLPaths(cert, key))
        assert fingerprint is not None
        assert len(fingerprint) == 2

    @pytest.mark.parametrize("missing", ["fullchain.pem", "privkey.pem"])
    def test_a_missing_file_yields_none(self, tmp_path: Path, missing: str) -> None:
        cert = tmp_path / "fullchain.pem"
        key = tmp_path / "privkey.pem"
        cert.write_text("cert")
        key.write_text("key")
        (tmp_path / missing).unlink()
        assert cert_mtimes(SSLPaths(cert, key)) is None

    def test_touching_a_file_changes_the_fingerprint(self, tmp_path: Path) -> None:
        cert = tmp_path / "fullchain.pem"
        key = tmp_path / "privkey.pem"
        cert.write_text("cert")
        key.write_text("key")
        paths = SSLPaths(cert, key)
        before = cert_mtimes(paths)
        stat = cert.stat()
        os.utime(cert, (stat.st_atime, stat.st_mtime + 10))
        assert cert_mtimes(paths) != before


class TestDecideCertAction:
    def test_exit_code_is_stable(self) -> None:
        # run.sh hardcodes this value in its supervisor loop — it is a
        # contract between the two files, not an implementation detail.
        assert CERT_RELOAD_EXIT_CODE == 86

    @pytest.mark.parametrize(
        ("running", "current", "new", "expected"),
        [
            # No source certificates anywhere: nothing to do.
            (False, None, None, "none"),
            # Sources appeared after a start without them: the staged
            # copies do not exist yet, so the process must exit for
            # run.sh to stage them (NOT start in-process).
            (False, None, (1.0, 1.0), "exit"),
            # Renewed while running (either file): exit for restaging.
            (True, (1.0, 1.0), (2.0, 1.0), "exit"),
            (True, (1.0, 1.0), (1.0, 2.0), "exit"),
            # Renewed while the listener happens to be down: same thing.
            (False, (1.0, 1.0), (2.0, 2.0), "exit"),
            # Crashed listener, sources unchanged: the staged copies are
            # still valid — recover in-process.
            (False, (1.0, 1.0), (1.0, 1.0), "start"),
            # Running and unchanged: leave it alone.
            (True, (1.0, 1.0), (1.0, 1.0), "none"),
            # Sources vanished mid-flight (renewal in progress): keep
            # serving the staged copies that are already loaded.
            (True, (1.0, 1.0), None, "none"),
            (False, (1.0, 1.0), None, "none"),
        ],
    )
    def test_decision_matrix(
        self,
        running: bool,
        current: tuple | None,
        new: tuple | None,
        expected: str,
    ) -> None:
        assert decide_cert_action(running, current, new) == expected


class TestConfigs:
    def test_main_config_serves_the_ingress_port_without_tls(self) -> None:
        config = build_main_config()
        assert config.port == MAIN_PORT
        assert config.host == "0.0.0.0"
        assert config.ssl_certfile is None
        assert config.access_log is False
        assert config.server_header is False

    def test_feed_config_enables_tls_and_hides_details(self, tmp_path: Path) -> None:
        paths = SSLPaths(tmp_path / "c.pem", tmp_path / "k.pem")
        config = build_feed_config(paths)
        assert config.port == FEED_PORT
        assert config.host == "0.0.0.0"
        assert config.ssl_certfile == str(paths.certfile)
        assert config.ssl_keyfile == str(paths.keyfile)
        # The token is part of the URL — it must never reach the access log.
        assert config.access_log is False
        assert config.server_header is False
        # Clients connect directly (router port forward): X-Forwarded-For
        # must never override the peer IP the rate limiter keys on.
        assert config.proxy_headers is False


# -- live listener tests -----------------------------------------------------


def _make_self_signed(directory: Path) -> SSLPaths:
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / "fullchain.pem"
    key = directory / "privkey.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return SSLPaths(cert, key)


def _stage_copies(source: SSLPaths, directory: Path) -> SSLPaths:
    """Copy the certificate pair like run.sh's stage_certs does."""
    directory.mkdir(parents=True, exist_ok=True)
    staged = SSLPaths(directory / "fullchain.pem", directory / "privkey.pem")
    shutil.copy(source.certfile, staged.certfile)
    shutil.copy(source.keyfile, staged.keyfile)
    return staged


async def _wait_for(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("condition not met in time")


def _bump_mtime(path: Path, seconds: float = 10.0) -> None:
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + seconds))


@pytest.fixture
def feed_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Storage:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    return Storage(default_db_path())


async def test_missing_certificates_keep_the_listener_off(
    feed_storage: Storage, tmp_path: Path
) -> None:
    # The calendar must never die because of the feed: without certificate
    # files the listener simply does not come up (error in the log).
    paths = SSLPaths(tmp_path / "missing.pem", tmp_path / "missing.key")
    listener = FeedListener(paths, host="127.0.0.1", port=0, check_interval=0.05)
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.sleep(0.3)
        assert not listener.running
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_unreadable_staged_copies_keep_the_listener_off(
    feed_storage: Storage, tmp_path: Path
) -> None:
    # Sources are stat-able but the staged copies are missing (staging
    # failed): starting uvicorn would crash on load_cert_chain, so the
    # listener must not be started at all.
    sources = SSLPaths(tmp_path / "fullchain.pem", tmp_path / "privkey.pem")
    sources.certfile.write_text("cert")
    sources.keyfile.write_text("key")
    staged = SSLPaths(tmp_path / "staged.pem", tmp_path / "staged.key")
    listener = FeedListener(
        staged, source_paths=sources, host="127.0.0.1", port=0, check_interval=1000
    )
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.sleep(0.3)
        assert not listener.running
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_run_returns_once_source_certificates_appear(
    feed_storage: Storage, tmp_path: Path
) -> None:
    # Certificates get provisioned while the add-on is already up. The
    # process cannot stage app-readable copies itself (the originals are
    # root-only), so run() must return -> exit 86 -> run.sh stages and
    # restarts. No real TLS involved: the listener never starts.
    sources = SSLPaths(tmp_path / "fullchain.pem", tmp_path / "privkey.pem")
    staged = SSLPaths(tmp_path / "staged.pem", tmp_path / "staged.key")
    listener = FeedListener(
        staged, source_paths=sources, host="127.0.0.1", port=0, check_interval=0.05
    )
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.sleep(0.2)
        assert not listener.running
        sources.certfile.write_text("cert")
        sources.keyfile.write_text("key")
        await asyncio.wait_for(asyncio.shield(task), timeout=15)
        assert task.exception() is None
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="braucht das openssl-CLI für ein Wegwerf-Testzertifikat",
)


@needs_openssl
async def test_feed_listener_serves_tls_and_exits_on_certificate_change(
    feed_storage: Storage, tmp_path: Path
) -> None:
    token = ensure_feed_token(feed_storage)
    sources = _make_self_signed(tmp_path / "ssl")
    staged = _stage_copies(sources, tmp_path / "staged")
    listener = FeedListener(
        staged, source_paths=sources, host="127.0.0.1", port=0, check_interval=0.05
    )
    task = asyncio.create_task(listener.run())
    try:
        await _wait_for(lambda: listener.started)
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(
                f"https://127.0.0.1:{listener.port}/feed/{token}.ics"
            )
        assert response.status_code == 200
        assert "BEGIN:VCALENDAR" in response.text
        assert response.headers["strict-transport-security"] == "max-age=31536000"

        # Simulated Let's Encrypt renewal on the SOURCE files: run() must
        # end cleanly (uvicorn only loads certificates at startup and the
        # fresh key needs restaging by root) and stop the server.
        _bump_mtime(sources.certfile)
        await asyncio.wait_for(asyncio.shield(task), timeout=15)
        assert task.exception() is None
        assert not listener.running
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@needs_openssl
async def test_serve_exits_with_the_reload_code_on_certificate_change(
    feed_storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Full launcher path: both listeners up, a renewed source certificate
    # must shut everything down and yield CERT_RELOAD_EXIT_CODE for the
    # run.sh supervisor loop.
    sources = _make_self_signed(tmp_path / "ssl")
    staged = _stage_copies(sources, tmp_path / "staged")
    monkeypatch.setenv("SSL_CERTFILE", str(staged.certfile))
    monkeypatch.setenv("SSL_KEYFILE", str(staged.keyfile))
    monkeypatch.setenv("SSL_SOURCE_CERTFILE", str(sources.certfile))
    monkeypatch.setenv("SSL_SOURCE_KEYFILE", str(sources.keyfile))
    monkeypatch.setattr(
        serve_module,
        "build_main_config",
        lambda: build_main_config(host="127.0.0.1", port=0),
    )
    serve_task = asyncio.create_task(
        serve(feed_host="127.0.0.1", feed_port=0, feed_check_interval=0.05)
    )
    try:
        # Give both listeners time to start and the initial certificate
        # check time to record the source fingerprint.
        await asyncio.sleep(0.5)
        assert not serve_task.done()
        _bump_mtime(sources.keyfile)
        exit_code = await asyncio.wait_for(asyncio.shield(serve_task), timeout=20)
        assert exit_code == CERT_RELOAD_EXIT_CODE
    finally:
        serve_task.cancel()
        with suppress(asyncio.CancelledError):
            await serve_task
