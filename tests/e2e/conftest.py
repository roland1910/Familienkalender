"""Fixtures for browser E2E tests: a live uvicorn server with seeded demo data.

The server runs against a temporary DATA_DIR filled by scripts/seed_demo.py,
with the periodic sync disabled (the demo sources have no real backends).
"""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from scripts.seed_demo import seed_demo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Screenshots land outside the packaged app; the directory is gitignored.
ARTIFACTS_DIR = REPO_ROOT / "tests" / "artifacts"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(url: str, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"uvicorn exited early with code {process.returncode}")
        try:
            if httpx.get(f"{url}/api/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise TimeoutError(f"server at {url} did not become healthy within {timeout}s")


@pytest.fixture(scope="session")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Base URL of a uvicorn instance serving the app with demo data."""
    data_dir = tmp_path_factory.mktemp("e2e-data")
    seed_demo(data_dir)
    port = _free_port()
    env = os.environ | {
        "DATA_DIR": str(data_dir),
        "SYNC_INTERVAL_SECONDS": "0",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        env=env,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(url, process)
        yield url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Kiosk display defaults: 1920x1080 with touch support."""
    return {
        **browser_context_args,
        "viewport": {"width": 1920, "height": 1080},
        "has_touch": True,
    }


@pytest.fixture(scope="session")
def artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR
