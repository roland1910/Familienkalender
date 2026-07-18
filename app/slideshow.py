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
import dataclasses
import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
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

    Order note: the explicit stack makes this LIFO, i.e. a depth-first walk
    with no defined ordering across siblings. That is irrelevant for a full
    scan, but near the ``limit`` (MAX_INDEXED_PHOTOS) it means *which* images
    get indexed is arbitrary — the walk stops at the cap wherever it happens
    to be, not at a stable, predictable subset. Acceptable here: the cap is a
    runaway guard, not a curation feature, and the rotation picks at random
    anyway.
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


# -- taken-at extraction (EXIF > filename > year folder) ---------------------

# The photo files are UNTRUSTED input (a network share anyone in the family
# writes to), so the EXIF parser below is deliberately paranoid: it reads at
# most MAX_EXIF_READ_BYTES, bounds-checks every offset it follows, and any
# structural surprise makes the whole extraction return None instead of
# raising. No image library is pulled in (requirements are hash-pinned);
# only the tiny TIFF/IFD walk needed for two ASCII date tags is implemented.
MAX_EXIF_READ_BYTES = 256 * 1024

_EXIF_TAG_DATETIME_ORIGINAL = 0x9003  # Exif IFD: moment the photo was taken
_EXIF_TAG_DATETIME = 0x0132  # IFD0 fallback: file change date in-camera
_EXIF_TAG_EXIF_IFD_POINTER = 0x8769
_EXIF_TYPE_ASCII = 2
# ASCII date values are "YYYY:MM:DD HH:MM:SS\0" (20 bytes); anything much
# longer is not a date tag we care about.
_MAX_ASCII_VALUE_LENGTH = 64

# Years accepted from *filename/folder* heuristics. Digital-camera EXIF is
# validated only for calendar validity; a random digit run in a name needs
# the stronger plausibility window to avoid false positives.
_MIN_PLAUSIBLE_YEAR = 1990

_EXIF_DATETIME_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")

# Filename patterns, tried in order. (?<!\d)/(?!\d) keep a longer digit run
# (a scan id, a phone number) from being misread as a date inside it.
_FILENAME_DATETIME_PATTERNS = (
    # IMG_20190816_173005 / PXL_20230102-070809 / bare 20190816-173005
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)"),
    # 2021-06-05 14.33.10 / 2021-06-05_14-33-10 / 2021-06-05 14:33:10
    re.compile(
        r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ _](\d{2})[.:\-](\d{2})[.:\-](\d{2})(?!\d)"
    ),
)
_FILENAME_DATE_PATTERNS = (
    # WhatsApp: IMG-20180923-WA0007 (date only)
    re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+"),
    # bare YYYYMMDD (plausibility-checked)
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),
)

_YEAR_FOLDER_RE = re.compile(r"(19|20)\d\d")


@dataclass(frozen=True)
class TakenAt:
    """A (possibly partial) moment a photo was taken.

    ``year`` is always present; the rest is filled as far as the source
    allows (EXIF: full, filename: date or date+time, year folder: year
    only). Serialized as-is into the ``taken`` field of /api/slideshow/next.
    """

    year: int
    month: int | None = None
    day: int | None = None
    hour: int | None = None
    minute: int | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _valid_date(year: int, month: int, day: int) -> bool:
    if not (_MIN_PLAUSIBLE_YEAR <= year <= date.today().year + 1):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _valid_time(hour: int, minute: int, second: int) -> bool:
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def _exif_datetime_string(data: bytes) -> str | None:
    """The raw EXIF date string from a JPEG byte prefix, or None.

    Walks the JPEG segment chain to the APP1 "Exif" segment, then the TIFF
    structure inside it: IFD0 -> Exif-IFD pointer -> DateTimeOriginal
    (0x9003), falling back to DateTime (0x0132) in IFD0. Every offset is
    bounds-checked; malformed input raises ValueError (callers catch).
    """
    if not data.startswith(b"\xff\xd8"):
        return None
    tiff = _find_exif_tiff(data)
    if tiff is None:
        return None
    return _tiff_datetime(tiff)


def _find_exif_tiff(data: bytes) -> bytes | None:
    """The TIFF blob of the first APP1 Exif segment, or None."""
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None  # not a marker where one must be — malformed
        marker = data[pos + 1]
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            pos += 2  # standalone marker without a length field
            continue
        if marker == 0xDA:
            return None  # start of scan — no EXIF before the image data
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2:
            return None
        segment = data[pos + 4 : pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return segment[6:]
        pos += 2 + length
    return None


def _tiff_datetime(tiff: bytes) -> str | None:
    """DateTimeOriginal (preferred) or DateTime from a TIFF blob, or None."""
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "little"
    elif tiff[:2] == b"MM":
        endian = "big"
    else:
        return None

    def u16(offset: int) -> int:
        if offset < 0 or offset + 2 > len(tiff):
            raise ValueError("EXIF offset out of bounds")
        return int.from_bytes(tiff[offset : offset + 2], endian)

    def u32(offset: int) -> int:
        if offset < 0 or offset + 4 > len(tiff):
            raise ValueError("EXIF offset out of bounds")
        return int.from_bytes(tiff[offset : offset + 4], endian)

    if u16(2) != 42:
        return None
    ifd0 = _read_ifd(u32(4), u16, u32)
    if _EXIF_TAG_EXIF_IFD_POINTER in ifd0:
        _, _, field_offset = ifd0[_EXIF_TAG_EXIF_IFD_POINTER]
        exif_ifd = _read_ifd(u32(field_offset), u16, u32)
        if _EXIF_TAG_DATETIME_ORIGINAL in exif_ifd:
            value = _ascii_value(tiff, exif_ifd[_EXIF_TAG_DATETIME_ORIGINAL], u32)
            if value:
                return value
    if _EXIF_TAG_DATETIME in ifd0:
        return _ascii_value(tiff, ifd0[_EXIF_TAG_DATETIME], u32)
    return None


def _read_ifd(offset: int, u16, u32) -> dict[int, tuple[int, int, int]]:
    """IFD entries as {tag: (type, count, offset-of-value-field)}.

    Only the 4-byte value/offset field *position* is recorded; whether it
    holds an inline value or a pointer is resolved by the value readers.
    Bounds violations raise ValueError (the entry count itself is a u16, so
    at most 65535 cheap iterations — no unbounded work on hostile input).
    """
    count = u16(offset)
    entries: dict[int, tuple[int, int, int]] = {}
    for index in range(count):
        base = offset + 2 + index * 12
        tag = u16(base)
        entry_type = u16(base + 2)
        value_count = u32(base + 4)
        entries[tag] = (entry_type, value_count, base + 8)
    return entries


def _ascii_value(tiff: bytes, entry: tuple[int, int, int], u32) -> str | None:
    """The ASCII value of an IFD entry (inline or offset-addressed)."""
    entry_type, count, field_offset = entry
    if entry_type != _EXIF_TYPE_ASCII or count == 0 or count > _MAX_ASCII_VALUE_LENGTH:
        return None
    if count <= 4:
        raw = tiff[field_offset : field_offset + count]
    else:
        value_offset = u32(field_offset)
        if value_offset + count > len(tiff):
            return None
        raw = tiff[value_offset : value_offset + count]
    return raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()


def _taken_from_exif(path: str) -> TakenAt | None:
    """Taken-at from JPEG EXIF, or None. Never raises (untrusted file)."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_EXIF_READ_BYTES)
    except OSError:
        return None
    try:
        value = _exif_datetime_string(data)
    except Exception:
        # Malformed/hostile EXIF — count it as unreadable, no path in the log.
        logger.debug("Unparsable EXIF data in a photo", exc_info=True)
        return None
    if not value:
        return None
    match = _EXIF_DATETIME_RE.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return None
    if not _valid_time(hour, minute, second):
        return None
    return TakenAt(year=year, month=month, day=day, hour=hour, minute=minute)


def _taken_from_filename(name: str) -> TakenAt | None:
    """Taken-at from well-known filename date patterns, or None."""
    for pattern in _FILENAME_DATETIME_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        year, month, day, hour, minute, second = (int(g) for g in match.groups())
        if not _valid_date(year, month, day):
            continue
        if _valid_time(hour, minute, second):
            return TakenAt(year=year, month=month, day=day, hour=hour, minute=minute)
        return TakenAt(year=year, month=month, day=day)  # date part still usable
    for pattern in _FILENAME_DATE_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        year, month, day = (int(g) for g in match.groups())
        if _valid_date(year, month, day):
            return TakenAt(year=year, month=month, day=day)
    return None


def _taken_from_folder(path: str, root: str) -> TakenAt | None:
    """Year from the first exact 19xx/20xx path segment below the media root."""
    try:
        relative = os.path.relpath(Path(path).parent, root)
    except ValueError:
        return None  # different drive on Windows dev machines
    if relative.startswith(".."):
        return None
    for segment in Path(relative).parts:
        if _YEAR_FOLDER_RE.fullmatch(segment):
            return TakenAt(year=int(segment))
    return None


def photo_taken_at(path: str, root: str | None = None) -> TakenAt | None:
    """Best-effort (possibly partial) taken-at moment for a photo, or None.

    Priority: EXIF DateTimeOriginal/DateTime (JPEG only — PNG/WebP carry no
    classic EXIF worth a bespoke parser) > filename date patterns > a
    year-named ancestor folder below the media root. When nothing can be
    extracted the answer is None and the frontend shows nothing — per
    Roland's call. Never raises; the file is untrusted input.
    """
    try:
        media = root if root is not None else media_root()
        if Path(path).suffix.lower() in {".jpg", ".jpeg"}:
            taken = _taken_from_exif(path)
            if taken is not None:
                return taken
        taken = _taken_from_filename(Path(path).name)
        if taken is not None:
            return taken
        return _taken_from_folder(path, media)
    except Exception:
        logger.debug("Taken-at extraction failed for a photo", exc_info=True)
        return None


# -- public slideshow API ----------------------------------------------------


@router.get("/next")
async def next_photo() -> dict:
    """A random not-yet-shown photo ``{id, name, taken}``; 404 on empty index.

    Marks the returned photo as shown (rotation with memory, see
    storage.pick_next_photo). When every photo has been shown the cycle
    resets automatically. ``taken`` is the (possibly partial) taken-at
    moment resolved at serve time (EXIF > filename > year folder) or null;
    the extraction reads the file, so it runs off the event loop. The
    stored path itself is never exposed.
    """
    picked = get_storage().pick_next_photo()
    if picked is None:
        raise HTTPException(status_code=404, detail="Keine Fotos im Index.")
    taken = await asyncio.to_thread(photo_taken_at, picked["path"])
    return {
        "id": picked["id"],
        "name": picked["name"],
        "taken": taken.as_dict() if taken is not None else None,
    }


def _path_is_below_media_root(path: str) -> bool:
    """Whether ``path`` resolves to the media root itself or a descendant.

    Uses ``os.path.realpath`` so a symlink pointing out of the tree, or a
    ``../`` sequence, is rejected on the *resolved* path — the same check
    ``normalize_media_dir`` applies at index time.
    """
    root = media_root()
    resolved = os.path.realpath(path)
    return resolved == root or os.path.commonpath([root, resolved]) == root


@router.get("/image/{photo_id}")
async def photo_image(photo_id: int) -> FileResponse:
    """Stream the image with the given DB id.

    Access is by id only — never a client path. A 404 (and index cleanup)
    if the id is unknown or the file vanished since the scan. The stored
    path was validated to live below the media root when it was indexed.

    Serve-time re-validation closes the TOCTOU window between index time and
    serve time (symmetric to the re-check in ``get_slideshow_dirs``): if the
    indexed file has since become a symlink, or now resolves outside the
    media root, it is rejected and the stale index entry dropped — the same
    treatment as a vanished file. Without this, a file swapped for a symlink
    to ``/etc/passwd`` after the scan would be streamed verbatim.
    """
    storage = get_storage()
    path = storage.get_photo_path(photo_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Foto nicht gefunden.")
    if (
        not Path(path).is_file()
        or Path(path).is_symlink()
        or not _path_is_below_media_root(path)
    ):
        # The file vanished, became a symlink, or now resolves outside the
        # media root since the last scan — drop the stale entry so the
        # rotation stops offering it, and never stream it.
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

    The response also carries ``media_root`` (the root boundary) and
    ``parent`` (the parent directory path, or ``null`` when already at the
    root) so the navigable admin browser can render a breadcrumb / "back"
    step without walking above the share — the root-boundary authority
    stays server-side, next to the traversal guard.
    """
    root = media_root()
    try:
        base = normalize_media_dir(path) if path else root
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
    # Parent is null at the root (cannot navigate above the share); below
    # the root the parent is always still within it, so no extra guard.
    parent = None if base == root else str(Path(base).parent)
    return {"media_root": root, "base": base, "parent": parent, "dirs": subdirs}
