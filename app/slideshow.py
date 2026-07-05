"""Photo slideshow API (/api/slideshow, /api/admin/slideshow).

The kiosk display can run a full-screen photo slideshow as a screensaver.
Photos live on a CIFS network share that Home Assistant mounts under
``/media`` (read-only for this add-on, see config.yaml map: media:ro and
apparmor.txt). The admin picks one or more directories below /media; a
recursive scan indexes every image file into the ``photos`` table, and the
frontend pulls one random unshown photo at a time.

Security model:

* Every configured/browsed directory is validated to live *below* the
  media root — normalized and resolved with ``os.path.realpath`` so a
  ``../`` sequence or a symlink pointing outside /media is rejected. This
  is the single guard against path traversal into the rest of the
  container filesystem.
* Images are served by DB id only (``GET /api/slideshow/image/{id}``),
  never by a client-supplied path. The id maps to a stored, already
  validated path; the file is streamed with a Content-Type derived from
  its extension.
* Like every other route these sit behind HA ingress plus the client-IP
  allowlist; the image endpoint additionally only ever exposes files that
  a prior scan (of an admin-approved directory) put into the index.

Scan strategy (114k+ real files): the recursive walk runs in a thread
executor via ``asyncio.to_thread`` so it never blocks the event loop, and
it is serialized by a lock so overlapping triggers (save + hourly timer)
collapse into one run. Symlinks are not followed (os.scandir with a
dir-entry symlink check), hidden and ``#recycle`` directories are skipped,
and per-directory errors are isolated so one unreadable folder does not
abort the whole scan.
"""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.storage import get_storage

logger = logging.getLogger(__name__)

# The HA media share mount point inside the add-on container. Overridable
# for local development and tests (which cannot mount a real /media).
DEFAULT_MEDIA_ROOT = "/media"

# Setting key: JSON array of directory paths (below the media root) the
# slideshow scans. Empty/missing means "no directories configured".
SLIDESHOW_DIRS_KEY = "slideshow_dirs"

# Recognized image extensions (case-insensitive). Everything else on the
# share (.mpg videos, #recycle junk, ...) is skipped by the scanner.
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

# Content types for the served extensions.
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

# Upper bound on indexed photos. The real share has ~114k images; this is
# deliberately generous but bounded, so a pathological tree (or a symlink
# loop that slipped past the no-follow guard) cannot grow the index and the
# SQLite file without limit. Once reached, the scan stops descending.
MAX_INDEXED_PHOTOS = 100_000

# Directories never descended into: version-control/system noise, the
# Synology recycle bin, and every dotfile directory.
_SKIP_DIR_NAMES = frozenset({"#recycle", "@eaDir"})

# Cap on the configured directory list — a handful is realistic; this
# rejects hostile bulk input before it is stored.
MAX_SLIDESHOW_DIRS = 50
# Per-path length cap (defense against pathological input in the setting).
MAX_DIR_PATH_LENGTH = 4096

router = APIRouter(prefix="/api/slideshow")
admin_router = APIRouter(
    prefix="/api/admin/slideshow", dependencies=[Depends(require_admin)]
)

# Serializes scans so save + hourly trigger never run concurrently.
_scan_lock = asyncio.Lock()


class InvalidMediaPathError(Exception):
    """A path is empty, malformed, or does not resolve below the media root."""


def media_root() -> str:
    """The media root as a realpath string (env override for tests)."""
    return os.path.realpath(os.environ.get("MEDIA_ROOT") or DEFAULT_MEDIA_ROOT)


def normalize_media_dir(raw: str) -> str:
    """Return the canonical realpath of ``raw`` if it is below the media root.

    Rejects (InvalidMediaPathError) empty input, overly long input, and any
    path whose resolved location is not the media root itself or a
    descendant of it. Uses ``os.path.realpath`` so both ``../`` traversal
    and symlinks pointing outside /media are caught: the check is on the
    *resolved* path, not the textual one. The media root itself is accepted
    (scanning all of /media is a valid, if broad, choice).
    """
    if not raw or not raw.strip():
        raise InvalidMediaPathError("Kein Pfad angegeben.")
    if len(raw) > MAX_DIR_PATH_LENGTH:
        raise InvalidMediaPathError("Pfad ist zu lang.")
    root = media_root()
    candidate = raw if Path(raw).is_absolute() else str(Path(root) / raw)
    resolved = os.path.realpath(candidate)
    # commonpath on the resolved paths: only accept root itself or below it.
    # (Guards against both "/media/../etc" and a symlink out of the tree.)
    if resolved != root and os.path.commonpath([root, resolved]) != root:
        raise InvalidMediaPathError(
            "Pfad liegt nicht unterhalb des Medienordners."
        )
    return resolved


def get_slideshow_dirs() -> list[str]:
    """The configured, still-valid slideshow directories (below the media root).

    Re-validated on read (defense in depth): a value smuggled into the
    settings table by another write path, or a directory that later became
    a symlink out of the tree, is dropped rather than scanned.
    """
    raw = get_storage().get_setting(SLIDESHOW_DIRS_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        if not isinstance(item, str):
            continue
        try:
            valid.append(normalize_media_dir(item))
        except InvalidMediaPathError:
            logger.warning("Dropping invalid stored slideshow dir: %r", item)
    return valid


def set_slideshow_dirs(dirs: list[str]) -> None:
    """Persist the slideshow directory list (validation happens in the API)."""
    get_storage().set_setting(SLIDESHOW_DIRS_KEY, json.dumps(dirs))


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    """Whether a directory name is skipped (hidden or on the skip list)."""
    return name.startswith(".") or name in _SKIP_DIR_NAMES


def iter_images(
    roots: list[str], *, limit: int = MAX_INDEXED_PHOTOS
) -> Iterator[tuple[str, float]]:
    """Yield (path, mtime) for every image below ``roots``, up to ``limit``.

    Recursive, iterative (an explicit stack, no recursion depth limit).
    Symlinked directories are not descended into (loop/escape guard).
    Hidden and skip-listed directories are pruned. Per-directory errors
    (permission denied, a folder that vanished mid-scan) are logged and
    skipped so one bad folder never aborts the whole walk.
    """
    yielded = 0
    stack = list(roots)
    while stack and yielded < limit:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            logger.warning("Skipping unreadable slideshow directory %r: %s", current, exc)
            continue
        for entry in entries:
            if yielded >= limit:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not _should_skip_dir(entry.name):
                        stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False) and _is_image(entry.name):
                    yield entry.path, entry.stat(follow_symlinks=False).st_mtime
                    yielded += 1
            except OSError as exc:
                # A single entry that vanished or is unreadable — skip it,
                # keep scanning the rest of the directory.
                logger.warning("Skipping unreadable entry %r: %s", entry.path, exc)


def _scan_sync() -> int:
    """Blocking scan of the configured dirs; replaces the index. Returns count."""
    dirs = get_slideshow_dirs()
    photos = list(iter_images(dirs))
    return get_storage().replace_photos(photos)


async def scan_photos() -> int:
    """Rescan all configured directories in a thread; returns the photo count.

    Serialized by ``_scan_lock`` so a save-triggered scan and the hourly
    scan never overlap. The blocking filesystem walk runs via
    ``asyncio.to_thread`` so the event loop stays responsive during the
    (potentially long, 114k-file) scan.
    """
    async with _scan_lock:
        count = await asyncio.to_thread(_scan_sync)
    logger.info("Slideshow index rebuilt: %d photos", count)
    return count


# The photo index is refreshed on a fixed cadence so files added to the
# share show up without an admin clicking "rescan"; the walk is cheap
# relative to the interval and runs off the event loop (see scan_photos).
PHOTO_SCAN_INTERVAL_SECONDS = 3600.0


async def periodic_photo_scan(interval: float = PHOTO_SCAN_INTERVAL_SECONDS) -> None:
    """Rescan the photo index once per ``interval`` while the app is up.

    A failing scan (e.g. the media share briefly unreachable) is logged and
    swallowed so the loop keeps running and recovers on the next tick.
    """
    while True:
        try:
            await scan_photos()
        except Exception:  # a scan failure must not kill the loop
            logger.exception("Periodic slideshow scan failed")
        await asyncio.sleep(interval)


# -- public slideshow API ----------------------------------------------------


@router.get("/next")
async def next_photo() -> dict:
    """A random not-yet-shown photo ``{id, name}``; 404 when the index is empty.

    Marks the returned photo as shown (rotation with memory, see
    storage.pick_next_photo). When every photo has been shown the cycle
    resets automatically.
    """
    picked = get_storage().pick_next_photo()
    if picked is None:
        raise HTTPException(status_code=404, detail="Keine Fotos im Index.")
    return picked


@router.get("/image/{photo_id}")
async def photo_image(photo_id: int) -> FileResponse:
    """Stream the image with the given DB id.

    Access is by id only — never a client path. A 404 (and index cleanup)
    if the id is unknown or the file vanished since the scan. The stored
    path was validated to live below the media root when it was indexed.
    """
    storage = get_storage()
    path = storage.get_photo_path(photo_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Foto nicht gefunden.")
    if not Path(path).is_file():
        # The file vanished since the last scan — drop the stale entry so
        # the rotation stops offering it.
        storage.delete_photo(photo_id)
        raise HTTPException(status_code=404, detail="Foto nicht mehr vorhanden.")
    media_type = _CONTENT_TYPES.get(
        Path(path).suffix.lower(), "application/octet-stream"
    )
    # Short private cache: the same image is requested once per rotation
    # step and may be preloaded, but must not be cached long behind ingress.
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=60"},
    )


# -- admin API ---------------------------------------------------------------


class SlideshowDirsUpdate(BaseModel):
    dirs: list[str] = Field(max_length=MAX_SLIDESHOW_DIRS)


def _slideshow_payload() -> dict:
    return {
        "dirs": get_slideshow_dirs(),
        "photo_count": get_storage().count_photos(),
        "media_root": media_root(),
    }


@admin_router.get("")
async def get_slideshow() -> dict:
    """Current slideshow directories and the indexed photo count."""
    return _slideshow_payload()


@admin_router.put("")
async def update_slideshow(update: SlideshowDirsUpdate) -> dict:
    """Replace the slideshow directory list and trigger a rescan.

    Each path is normalized and validated to live below the media root
    (400, German message, with the offending path). Duplicates are
    collapsed. The rescan runs after saving so the returned photo count
    already reflects the new directories.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in update.dirs:
        try:
            path = normalize_media_dir(raw)
        except InvalidMediaPathError as exc:
            raise HTTPException(
                status_code=400, detail=f"{raw!r}: {exc}"
            ) from exc
        if path not in seen:
            seen.add(path)
            normalized.append(path)
    set_slideshow_dirs(normalized)
    await scan_photos()
    return _slideshow_payload()


@admin_router.post("/rescan")
async def rescan_slideshow() -> dict:
    """Re-read the configured directories from disk and rebuild the index."""
    await scan_photos()
    return _slideshow_payload()


@admin_router.get("/dirs")
async def list_dirs(path: str = Query(default="")) -> dict:
    """List immediate subdirectories of ``path`` (below the media root).

    ``path`` empty means the media root itself. The path is validated to
    live below the media root (400 otherwise), so the browser can never
    walk out of the share. Hidden and skip-listed directories are omitted.
    Entries are returned as ``{name, path}`` with absolute (validated)
    paths so the frontend can pass one straight back as a slideshow dir.
    """
    try:
        base = normalize_media_dir(path) if path else media_root()
    except InvalidMediaPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not Path(base).is_dir():
        raise HTTPException(status_code=404, detail="Verzeichnis nicht gefunden.")
    subdirs = []
    try:
        for entry in os.scandir(base):
            if (
                entry.is_dir(follow_symlinks=False)
                and not _should_skip_dir(entry.name)
            ):
                subdirs.append({"name": entry.name, "path": entry.path})
    except OSError as exc:
        raise HTTPException(
            status_code=502, detail=f"Verzeichnis nicht lesbar: {exc}"
        ) from exc
    subdirs.sort(key=lambda item: item["name"].lower())
    return {"base": base, "dirs": subdirs}
