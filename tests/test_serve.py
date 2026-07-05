"""Tests for the launcher (app.serve): two uvicorn listeners, one process.

The pure decision logic (SSL path resolution, certificate mtime
fingerprint, start/restart decision, uvicorn configs) is unit-tested
everywhere. The real TLS handshake and the restart-on-renewal behavior
run against a live listener with an on-the-fly self-signed certificate;
those tests need the openssl CLI (present in Git Bash on the dev
machine) and are skipped without it — the final TLS verification point
is the Pi deployment either way.
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

from app.serve import (
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
    @pytest.mark.parametrize(
        ("running", "current", "new", "expected"),
        [
            # Not running, no certificates: nothing to do (error was logged).
            (False, None, None, "none"),
            # Not running, certificates (newly) present: start.
            (False, None, (1.0, 1.0), "start"),
            # Also after a crash with unchanged files: start again.
            (False, (1.0, 1.0), (1.0, 1.0), "start"),
            # Running and unchanged: leave it alone.
            (True, (1.0, 1.0), (1.0, 1.0), "none"),
            # Running and renewed: restart with the new certificate.
            (True, (1.0, 1.0), (2.0, 1.0), "restart"),
            (True, (1.0, 1.0), (1.0, 2.0), "restart"),
            # Files vanished mid-flight (renewal in progress): keep serving
            # the certificate that is already loaded in memory.
            (True, (1.0, 1.0), None, "none"),
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


async def _wait_for(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("condition not met in time")


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
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="braucht das openssl-CLI für ein Wegwerf-Testzertifikat",
)


@needs_openssl
async def test_feed_listener_serves_tls_and_restarts_on_certificate_change(
    feed_storage: Storage, tmp_path: Path
) -> None:
    token = ensure_feed_token(feed_storage)
    paths = _make_self_signed(tmp_path)
    listener = FeedListener(paths, host="127.0.0.1", port=0, check_interval=0.05)
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

        # Simulated Let's Encrypt renewal: a bumped mtime must restart the
        # listener (uvicorn only loads certificates at startup).
        stat = paths.certfile.stat()
        os.utime(paths.certfile, (stat.st_atime, stat.st_mtime + 10))
        await _wait_for(lambda: listener.restarts >= 1 and listener.started)
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(
                f"https://127.0.0.1:{listener.port}/feed/{token}.ics"
            )
        assert response.status_code == 200
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@needs_openssl
async def test_listener_starts_once_certificates_appear(
    feed_storage: Storage, tmp_path: Path
) -> None:
    cert_dir = tmp_path / "ssl"
    cert_dir.mkdir()
    paths = SSLPaths(cert_dir / "fullchain.pem", cert_dir / "privkey.pem")
    listener = FeedListener(paths, host="127.0.0.1", port=0, check_interval=0.05)
    task = asyncio.create_task(listener.run())
    try:
        await asyncio.sleep(0.2)
        assert not listener.running
        # Certificates get provisioned while the add-on is already up.
        staging = tmp_path / "staging"
        staging.mkdir()
        generated = _make_self_signed(staging)
        shutil.copy(generated.certfile, paths.certfile)
        shutil.copy(generated.keyfile, paths.keyfile)
        await _wait_for(lambda: listener.started)
        token = ensure_feed_token(feed_storage)
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.get(
                f"https://127.0.0.1:{listener.port}/feed/{token}.ics"
            )
        assert response.status_code == 200
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
