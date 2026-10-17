"""TEMPORARY diagnostic sink (17.10.2026) — remove once the cause is found.

Why this exists
---------------
Roland's kiosk stops reacting to touch after a while: no operating icons,
no calendar, while the slideshow keeps advancing. Measured on the device,
xdotool MOUSE clicks are just as ineffective in that state, so it is not a
touch problem — the page simply stops acting on input although its timers
keep running. A ``refresh_browser`` cures it instantly.

Every channel that would normally show the inside of the page is closed
here: the kiosk browser (WebKitGTK/luakit) has no developer tools, its
screenshot endpoint is broken, and ``document.title`` never reaches the
window title because the app runs inside the Ingress **iframe**. What is
left is the app's own backend: the page posts short state lines here and
they are appended to a file that can be read from the host.

Deliberately NOT admin-gated: the reports come from the kiosk, which runs
as an ordinary (non-admin) Home Assistant user. The IP allowlist in
``app/auth.py`` still applies, so only the Ingress proxy and localhost can
reach it. The payload is foreign input and is treated as such: hard length
limit, control characters stripped (a report must not be able to forge
extra log lines), and a cap on the file so a loop can never fill the disk.
"""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request

from app.storage import resolve_data_dir

router = APIRouter(prefix="/api/_diag")

# One report is a short state line; anything longer is a bug or an attempt.
MAX_REPORT_CHARS = 300
# Roughly a day of heartbeats plus every interaction, with room to spare.
MAX_LOG_BYTES = 2_000_000
LOG_FILENAME = "diag.log"

# Keep printable text only. Newlines are the separator of the log format, so
# letting them through would let a report pretend to be several entries.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(raw: str) -> str:
    """One log-safe line: control characters out, length capped."""
    return _UNSAFE.sub(" ", raw)[:MAX_REPORT_CHARS].strip()


def log_path():
    return resolve_data_dir() / LOG_FILENAME


@router.post("")
async def report(request: Request) -> dict:
    """Append one state line from the kiosk page to the diagnostic log."""
    body = (await request.body())[:MAX_REPORT_CHARS * 4]
    line = sanitize(body.decode("utf-8", "replace"))
    if not line:
        return {"ok": True}
    path = log_path()
    try:
        # Start over rather than grow without end; the interesting part is
        # always the most recent stretch before a wedge.
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            path.unlink()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(tz=UTC).isoformat(timespec='seconds')} {line}\n")
    except OSError:
        # A diagnostic must never break the page it observes.
        return {"ok": False}
    return {"ok": True}
