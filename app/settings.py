"""Typed access to persisted admin settings.

Settings live in the SQLite ``settings`` table (key/value, see
app.storage). This module owns the known keys and the parsing/fallback
logic so API and sync code never deal with raw strings.
"""

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import time

from app.filtering import DEFAULT_EVENING_BOUNDARY
from app.storage import Storage

logger = logging.getLogger(__name__)

EVENING_BOUNDARY_KEY = "evening_boundary"
# Google OAuth app credentials (Desktop client). The client secret is a
# secret: it must never be returned by any GET API (see app.admin).
GOOGLE_CLIENT_ID_KEY = "google_client_id"
GOOGLE_CLIENT_SECRET_KEY = "google_client_secret"
# Smart-plug sensors shown as the device list in the power view, stored as
# a JSON array of {"entity_id", "name"} objects.
POWER_DEVICES_KEY = "power_devices"
# URL token protecting the subscribable ICS feed (GET /feed/<token>.ics).
# It is the sole auth on the dedicated feed port, so it is generated with
# plenty of entropy and never returned by any non-admin endpoint.
FEED_TOKEN_KEY = "feed_token"
# Public hostname shown in the admin UI's subscription URL (the router
# forwards external port 8098 to the feed listener). Bare host only —
# no scheme, port or path; empty/missing falls back to the request host.
FEED_PUBLIC_HOST_KEY = "feed_public_host"
# One-way "Busy MV" sync (MoreValue → Xalt primary calendar): master
# on/off switch, the source ids whose events are mirrored, and the last-run
# status (JSON, error already sanitized). See app.busy_sync.
BUSY_SYNC_ENABLED_KEY = "busy_sync_enabled"
BUSY_SYNC_SOURCE_IDS_KEY = "busy_sync_source_ids"
BUSY_SYNC_STATUS_KEY = "busy_sync_status"
# One-way mirror sync (Xalt → MoreValue, see app.mirror_sync): master
# on/off switch, the source ids whose appointments are copied, the id of the
# CalDAV source whose calendar receives the copies, and the last-run status
# (JSON, error already sanitized).
MIRROR_SYNC_ENABLED_KEY = "mirror_sync_enabled"
MIRROR_SYNC_SOURCE_IDS_KEY = "mirror_sync_source_ids"
MIRROR_SYNC_TARGET_KEY = "mirror_sync_target_source_id"
MIRROR_SYNC_STATUS_KEY = "mirror_sync_status"
# Copies waiting to be removed from a calendar that is no longer the mirror
# target (target switched, or Roland asked for a clean stop). Persisted rather
# than done inside the admin request: removing a few hundred CalDAV resources
# takes far too long to keep an admin page waiting, and a queue survives a
# restart in the middle of it. Drained at the start of the next mirror run.
MIRROR_SYNC_CLEANUP_KEY = "mirror_sync_cleanup"
# Birthday sync (contact birthdays → Xalt and/or a CalDAV calendar, see
# app.birthday_sync): master on/off switch, the google_contacts source ids
# whose birthdays are written out, the two independently switchable targets
# (the Google write token's primary calendar, and a CalDAV source's calendar)
# and the last-run status (JSON, error already sanitized).
BIRTHDAY_SYNC_ENABLED_KEY = "birthday_sync_enabled"
BIRTHDAY_SYNC_SOURCE_IDS_KEY = "birthday_sync_source_ids"
BIRTHDAY_SYNC_GOOGLE_KEY = "birthday_sync_google"
BIRTHDAY_SYNC_CALDAV_TARGET_KEY = "birthday_sync_caldav_target_id"
BIRTHDAY_SYNC_STATUS_KEY = "birthday_sync_status"
# Server-side default calendar view (month/week) for devices without a
# per-device choice in localStorage — the kiosk browser loses its storage
# on every restart, so the initial view must come from the server.
DEFAULT_VIEW_KEY = "default_view"

# Server-side default for the photo-slideshow screensaver ("on"/"off") for
# devices without a per-device choice in localStorage — same rationale as
# default_view: the kiosk browser loses its storage on every restart, and
# Roland wants the screensaver armed there without a manual tap.
SCREENSAVER_DEFAULT_KEY = "screensaver_default"

# Whether the slideshow also plays the videos in the index ("on"/"off").
# Videos are ALWAYS indexed (re-walking ~114k files on every toggle would be
# absurd); this switch only decides whether /api/slideshow/next is allowed to
# hand one out. Default "off": decoding phone videos off the CIFS share can
# stutter on the Pi, so Roland turns it on deliberately.
SLIDESHOW_VIDEOS_KEY = "slideshow_videos"

# The calendar views the frontend knows; mirrors VIEWS in
# app/static/js/view-memory.js.
CALENDAR_VIEWS = ("month", "week")
FALLBACK_VIEW = "month"

# Screensaver default states; mirrors resolveScreensaverEnabled in
# app/static/js/screensaver-memory.js.
SCREENSAVER_DEFAULTS = ("on", "off")
FALLBACK_SCREENSAVER_DEFAULT = "off"

# Slideshow video playback states and the conservative default.
SLIDESHOW_VIDEO_STATES = ("on", "off")
FALLBACK_SLIDESHOW_VIDEOS = "off"

# DNS limits: 253 chars total, labels of 1-63 chars, letters/digits/
# hyphens, no leading/trailing hyphen. Also matches plain IPv4 literals.
MAX_PUBLIC_HOST_LENGTH = 253
_HOST_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
PUBLIC_HOST_PATTERN = re.compile(rf"^{_HOST_LABEL}(\.{_HOST_LABEL})*$", re.IGNORECASE)

# HA entity ids are lowercase domain.object_id. Shared between the admin API
# (validates on write) and get_power_devices (defense in depth on read, in
# case the stored value was ever written by a future/other code path).
POWER_ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# HA entity ids in practice are far shorter; this is a defensive upper bound
# against pathological input, not a realistic sensor name length.
MAX_POWER_ENTITY_ID_LENGTH = 255


def is_valid_power_entity_id(entity_id: str) -> bool:
    """Whether entity_id is a plausible HA sensor entity id for the power view."""
    return (
        len(entity_id) <= MAX_POWER_ENTITY_ID_LENGTH
        and POWER_ENTITY_ID_PATTERN.fullmatch(entity_id) is not None
    )


@dataclass(frozen=True)
class PowerDevice:
    """One device row of the power view: an HA sensor plus a display name.

    ``name`` is an optional override. When empty, the power view uses the
    sensor's HA ``friendly_name`` instead (see app.power / power-view.js).
    """

    entity_id: str
    name: str = ""


# The household's smart plugs with German display names — used until the
# list is edited in the admin UI.
DEFAULT_POWER_DEVICES = (
    PowerDevice("sensor.kuhlschrank_leistung", "Kühlschrank"),
    PowerDevice("sensor.tv_sideboard_leistung", "TV-Sideboard"),
    PowerDevice("sensor.spuhlmaschiene_leistung", "Spülmaschine"),
    PowerDevice("sensor.schreibtisch_leistung", "Schreibtisch"),
    PowerDevice("sensor.matter_over_wifi_smart_plug_6_leistung", "Steckdose 6"),
)


def get_evening_boundary(storage: Storage) -> time:
    """Evening boundary for the family filter (HH:MM).

    Resolution order: persisted admin setting → EVENING_BOUNDARY env var
    (kept as a fallback for tests and local development without a DB) →
    default 17:00. Invalid values fall through to the next stage.
    """
    candidates = (storage.get_setting(EVENING_BOUNDARY_KEY), os.environ.get("EVENING_BOUNDARY"))
    for raw in candidates:
        if raw:
            try:
                return time.fromisoformat(raw)
            except ValueError:
                continue
    return DEFAULT_EVENING_BOUNDARY


def is_valid_default_view(view: str) -> bool:
    """Whether view is a calendar view the frontend can start in."""
    return view in CALENDAR_VIEWS


def get_default_view(storage: Storage) -> str:
    """The configured default calendar view; anything invalid yields "month".

    Re-validated on read (defense in depth): a value written by another
    path must never push an unknown view name into the frontend.
    """
    raw = storage.get_setting(DEFAULT_VIEW_KEY)
    if raw and is_valid_default_view(raw):
        return raw
    if raw:
        logger.warning("Ignoring invalid stored default view: %r", raw)
    return FALLBACK_VIEW


def set_default_view(storage: Storage, view: str) -> None:
    """Persist the default calendar view (validation happens in the API layer)."""
    storage.set_setting(DEFAULT_VIEW_KEY, view)


def is_valid_screensaver_default(value: str) -> bool:
    """Whether value is a known screensaver default state ("on"/"off")."""
    return value in SCREENSAVER_DEFAULTS


def get_screensaver_default(storage: Storage) -> str:
    """The configured screensaver default; anything invalid yields "off".

    Re-validated on read (defense in depth): a value written by another
    path must never arm the screensaver on every device by accident.
    """
    raw = storage.get_setting(SCREENSAVER_DEFAULT_KEY)
    if raw and is_valid_screensaver_default(raw):
        return raw
    if raw:
        logger.warning("Ignoring invalid stored screensaver default: %r", raw)
    return FALLBACK_SCREENSAVER_DEFAULT


def set_screensaver_default(storage: Storage, value: str) -> None:
    """Persist the screensaver default (validation happens in the API layer)."""
    storage.set_setting(SCREENSAVER_DEFAULT_KEY, value)


def is_valid_slideshow_videos(value: str) -> bool:
    """Whether value is a known slideshow video state ("on"/"off")."""
    return value in SLIDESHOW_VIDEO_STATES


def get_slideshow_videos(storage: Storage) -> str:
    """The configured slideshow video state; anything invalid yields "off".

    Re-validated on read (defense in depth): a value written by another path
    must never start handing out videos the admin did not ask for.
    """
    raw = storage.get_setting(SLIDESHOW_VIDEOS_KEY)
    if raw and is_valid_slideshow_videos(raw):
        return raw
    if raw:
        logger.warning("Ignoring invalid stored slideshow video state: %r", raw)
    return FALLBACK_SLIDESHOW_VIDEOS


def set_slideshow_videos(storage: Storage, value: str) -> None:
    """Persist the slideshow video state (validation happens in the API layer)."""
    storage.set_setting(SLIDESHOW_VIDEOS_KEY, value)


def get_feed_token(storage: Storage) -> str | None:
    """The current feed token, or None while none has been generated yet."""
    return storage.get_setting(FEED_TOKEN_KEY)


def ensure_feed_token(storage: Storage) -> str:
    """The current feed token, generating (and persisting) one if missing."""
    token = storage.get_setting(FEED_TOKEN_KEY)
    if token:
        return token
    return rotate_feed_token(storage)


def rotate_feed_token(storage: Storage) -> str:
    """Replace the feed token with a fresh one — old feed URLs stop working."""
    token = secrets.token_urlsafe(32)
    storage.set_setting(FEED_TOKEN_KEY, token)
    return token


def is_valid_public_host(host: str) -> bool:
    """Whether host is a bare hostname/IPv4 usable in the feed URL.

    Deliberately ASCII-only (internationalized names go in as punycode) —
    the value ends up verbatim in a URL shown by the admin UI.
    """
    return (
        0 < len(host) <= MAX_PUBLIC_HOST_LENGTH
        and PUBLIC_HOST_PATTERN.fullmatch(host) is not None
    )


def get_feed_public_host(storage: Storage) -> str | None:
    """The configured public feed host, or None to use the request host.

    Re-validated on read (defense in depth): a value smuggled into the
    settings table by another write path must not leak into generated URLs.
    """
    raw = storage.get_setting(FEED_PUBLIC_HOST_KEY)
    if raw and is_valid_public_host(raw):
        return raw
    if raw:
        logger.warning("Ignoring invalid stored feed public host: %r", raw)
    return None


def set_feed_public_host(storage: Storage, host: str) -> None:
    """Persist the public feed host; an empty value clears the override
    (validation happens in the API layer)."""
    storage.set_setting(FEED_PUBLIC_HOST_KEY, host)


def is_busy_sync_enabled(storage: Storage) -> bool:
    """Whether the one-way Busy MV sync is switched on (default off)."""
    return storage.get_setting(BUSY_SYNC_ENABLED_KEY) == "1"


def set_busy_sync_enabled(storage: Storage, enabled: bool) -> None:
    """Persist the Busy MV sync on/off switch."""
    storage.set_setting(BUSY_SYNC_ENABLED_KEY, "1" if enabled else "0")


def _get_source_id_list(storage: Storage, key: str) -> list[int]:
    """A stored JSON list of source ids, defensively filtered.

    A missing or unparseable value yields the empty list (nothing selected);
    non-int entries (including bools) are skipped rather than coerced.
    """
    raw = storage.get_setting(key)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    return [int(item) for item in items if isinstance(item, int) and not isinstance(item, bool)]


def _set_source_id_list(storage: Storage, key: str, source_ids: list[int]) -> None:
    """Persist a source id list (deduplicated, order preserved)."""
    storage.set_setting(key, json.dumps(list(dict.fromkeys(source_ids))))


def get_busy_sync_source_ids(storage: Storage) -> list[int]:
    """The source ids whose events are mirrored as Busy MV blocks."""
    return _get_source_id_list(storage, BUSY_SYNC_SOURCE_IDS_KEY)


def set_busy_sync_source_ids(storage: Storage, source_ids: list[int]) -> None:
    """Persist the mirrored-source id list (deduplicated, order preserved)."""
    _set_source_id_list(storage, BUSY_SYNC_SOURCE_IDS_KEY, source_ids)


def get_busy_sync_status(storage: Storage) -> dict:
    """The last Busy-sync status dict (empty when the sync never ran).

    Shape: {"last_run": iso|None, "active_blocks": int, "error": str|None}.
    """
    raw = storage.get_setting(BUSY_SYNC_STATUS_KEY)
    if not raw:
        return {"last_run": None, "active_blocks": 0, "error": None}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"last_run": None, "active_blocks": 0, "error": None}
    return {
        "last_run": data.get("last_run"),
        "active_blocks": int(data.get("active_blocks", 0) or 0),
        "error": data.get("error"),
    }


def set_busy_sync_status(
    storage: Storage, *, last_run: str, active_blocks: int, error: str | None
) -> None:
    """Persist the Busy-sync status (error must already be sanitized)."""
    storage.set_setting(
        BUSY_SYNC_STATUS_KEY,
        json.dumps(
            {"last_run": last_run, "active_blocks": active_blocks, "error": error}
        ),
    )


def is_mirror_sync_enabled(storage: Storage) -> bool:
    """Whether the one-way Xalt → MoreValue mirror is switched on (default off)."""
    return storage.get_setting(MIRROR_SYNC_ENABLED_KEY) == "1"


def set_mirror_sync_enabled(storage: Storage, enabled: bool) -> None:
    """Persist the mirror-sync on/off switch."""
    storage.set_setting(MIRROR_SYNC_ENABLED_KEY, "1" if enabled else "0")


def get_mirror_sync_source_ids(storage: Storage) -> list[int]:
    """The source ids whose appointments are copied into the target calendar."""
    return _get_source_id_list(storage, MIRROR_SYNC_SOURCE_IDS_KEY)


def set_mirror_sync_source_ids(storage: Storage, source_ids: list[int]) -> None:
    """Persist the mirrored-source id list (deduplicated, order preserved)."""
    _set_source_id_list(storage, MIRROR_SYNC_SOURCE_IDS_KEY, source_ids)


def get_mirror_sync_target_source_id(storage: Storage) -> int | None:
    """The CalDAV source whose calendar receives the copies, or None.

    Deliberately a source id, not a URL: the add-on writes into the already
    configured (and SSRF-validated) collection of an existing CalDAV source,
    so no free-form target URL ever enters the write path.
    """
    raw = storage.get_setting(MIRROR_SYNC_TARGET_KEY)
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Ignoring invalid stored mirror target: %r", raw)
        return None


def set_mirror_sync_target_source_id(storage: Storage, source_id: int | None) -> None:
    """Persist the mirror target source id; None clears it."""
    if source_id is None:
        storage.delete_setting(MIRROR_SYNC_TARGET_KEY)
        return
    storage.set_setting(MIRROR_SYNC_TARGET_KEY, str(int(source_id)))


def get_mirror_sync_status(storage: Storage) -> dict:
    """The last mirror-sync status dict (zeroed when the sync never ran).

    Shape: {"last_run": iso|None, "active_mirrors": int, "conflicts": int,
    "error": str|None, "skipped": bool, "skip_reason": str|None}.

    ``skipped`` marks a run the data-loss guard held back (see
    app.mirror_sync); it is deliberately NOT an error — nothing failed, the
    run simply refused to delete on an untrustworthy basis.
    """
    empty = {
        "last_run": None,
        "active_mirrors": 0,
        "conflicts": 0,
        "error": None,
        "skipped": False,
        "skip_reason": None,
    }
    raw = storage.get_setting(MIRROR_SYNC_STATUS_KEY)
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return empty
    return {
        "last_run": data.get("last_run"),
        "active_mirrors": int(data.get("active_mirrors", 0) or 0),
        "conflicts": int(data.get("conflicts", 0) or 0),
        "error": data.get("error"),
        "skipped": bool(data.get("skipped")),
        "skip_reason": data.get("skip_reason"),
    }


def set_mirror_sync_status(
    storage: Storage,
    *,
    last_run: str,
    active_mirrors: int,
    conflicts: int,
    error: str | None,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> None:
    """Persist the mirror-sync status (error must already be sanitized)."""
    storage.set_setting(
        MIRROR_SYNC_STATUS_KEY,
        json.dumps(
            {
                "last_run": last_run,
                "active_mirrors": active_mirrors,
                "conflicts": conflicts,
                "error": error,
                "skipped": skipped,
                "skip_reason": skip_reason,
            }
        ),
    )


def get_mirror_sync_cleanup(storage: Storage) -> dict:
    """The pending mirror-cleanup queue.

    Shape: ``{"pending": [{"source_id": int, "attempts": int}, …],
    "failed": bool}``. ``pending`` names the CalDAV SOURCES whose calendars
    still hold copies the add-on should remove (never a URL — the cleanup
    writes into an existing source's already validated collection, exactly
    like the mirror itself). ``failed`` marks that a queued cleanup was given
    up on, so the admin UI can ask Roland to look into the old calendar.

    Defensive on read like every other stored setting: unparseable JSON and
    entries without a numeric source id are dropped instead of breaking the
    drain (which deletes in a real calendar).
    """
    empty: dict = {"pending": [], "failed": False}
    raw = storage.get_setting(MIRROR_SYNC_CLEANUP_KEY)
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return empty
    if not isinstance(data, dict):
        return empty
    pending = []
    for entry in data.get("pending") or []:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        if not isinstance(source_id, int) or isinstance(source_id, bool):
            continue
        attempts = entry.get("attempts")
        pending.append(
            {
                "source_id": source_id,
                "attempts": attempts if isinstance(attempts, int) else 0,
            }
        )
    return {"pending": pending, "failed": bool(data.get("failed"))}


def set_mirror_sync_cleanup(
    storage: Storage, *, pending: list[dict], failed: bool
) -> None:
    """Persist the pending mirror-cleanup queue."""
    storage.set_setting(
        MIRROR_SYNC_CLEANUP_KEY,
        json.dumps({"pending": pending, "failed": failed}),
    )


def queue_mirror_sync_cleanup(storage: Storage, source_id: int) -> None:
    """Remember that ``source_id``'s calendar still holds copies to remove.

    Idempotent (queueing the same source twice keeps one entry) and clears a
    previous failure note: a fresh order deserves a fresh verdict.
    """
    state = get_mirror_sync_cleanup(storage)
    pending = state["pending"]
    if not any(entry["source_id"] == source_id for entry in pending):
        pending.append({"source_id": source_id, "attempts": 0})
    set_mirror_sync_cleanup(storage, pending=pending, failed=False)


def drop_mirror_sync_cleanup(storage: Storage, source_id: int) -> None:
    """Forget a queued cleanup (the source itself is gone — unreachable)."""
    state = get_mirror_sync_cleanup(storage)
    set_mirror_sync_cleanup(
        storage,
        pending=[e for e in state["pending"] if e["source_id"] != source_id],
        failed=state["failed"],
    )


def is_birthday_sync_enabled(storage: Storage) -> bool:
    """Whether the birthday sync is switched on (default off)."""
    return storage.get_setting(BIRTHDAY_SYNC_ENABLED_KEY) == "1"


def set_birthday_sync_enabled(storage: Storage, enabled: bool) -> None:
    """Persist the birthday-sync on/off switch."""
    storage.set_setting(BIRTHDAY_SYNC_ENABLED_KEY, "1" if enabled else "0")


def get_birthday_sync_source_ids(storage: Storage) -> list[int]:
    """The contact-source ids whose birthdays are written into the targets."""
    return _get_source_id_list(storage, BIRTHDAY_SYNC_SOURCE_IDS_KEY)


def set_birthday_sync_source_ids(storage: Storage, source_ids: list[int]) -> None:
    """Persist the birthday-source id list (deduplicated, order preserved)."""
    _set_source_id_list(storage, BIRTHDAY_SYNC_SOURCE_IDS_KEY, source_ids)


def is_birthday_sync_google_enabled(storage: Storage) -> bool:
    """Whether birthdays go into the Google (Xalt) calendar (default off).

    Independent of the CalDAV target: Roland can pick either, both or — with
    the master switch on but no target — none, in which case the sync simply
    has nothing to do.
    """
    return storage.get_setting(BIRTHDAY_SYNC_GOOGLE_KEY) == "1"


def set_birthday_sync_google_enabled(storage: Storage, enabled: bool) -> None:
    """Persist the Google target switch of the birthday sync."""
    storage.set_setting(BIRTHDAY_SYNC_GOOGLE_KEY, "1" if enabled else "0")


def get_birthday_sync_caldav_target_id(storage: Storage) -> int | None:
    """The CalDAV source whose calendar receives the birthday series, or None.

    A source id, never a URL — same rationale as the mirror target: the
    add-on only ever writes into an already configured, SSRF-validated
    collection.
    """
    raw = storage.get_setting(BIRTHDAY_SYNC_CALDAV_TARGET_KEY)
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Ignoring invalid stored birthday CalDAV target: %r", raw)
        return None


def set_birthday_sync_caldav_target_id(storage: Storage, source_id: int | None) -> None:
    """Persist the birthday CalDAV target source id; None clears it."""
    if source_id is None:
        storage.delete_setting(BIRTHDAY_SYNC_CALDAV_TARGET_KEY)
        return
    storage.set_setting(BIRTHDAY_SYNC_CALDAV_TARGET_KEY, str(int(source_id)))


def get_birthday_sync_status(storage: Storage) -> dict:
    """The last birthday-sync status dict (zeroed when it never ran).

    Shape: {"last_run": iso|None, "active_google": int, "active_caldav": int,
    "conflicts": int, "error": str|None, "skipped": bool,
    "skip_reason": str|None}. ``skipped`` marks a run the data-loss guard
    held back — deliberately NOT an error (see app.sync_guard).
    """
    empty = {
        "last_run": None,
        "active_google": 0,
        "active_caldav": 0,
        "conflicts": 0,
        "error": None,
        "skipped": False,
        "skip_reason": None,
    }
    raw = storage.get_setting(BIRTHDAY_SYNC_STATUS_KEY)
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return empty
    return {
        "last_run": data.get("last_run"),
        "active_google": int(data.get("active_google", 0) or 0),
        "active_caldav": int(data.get("active_caldav", 0) or 0),
        "conflicts": int(data.get("conflicts", 0) or 0),
        "error": data.get("error"),
        "skipped": bool(data.get("skipped")),
        "skip_reason": data.get("skip_reason"),
    }


def set_birthday_sync_status(
    storage: Storage,
    *,
    last_run: str,
    active_google: int,
    active_caldav: int,
    conflicts: int = 0,
    error: str | None,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> None:
    """Persist the birthday-sync status (error must already be sanitized)."""
    storage.set_setting(
        BIRTHDAY_SYNC_STATUS_KEY,
        json.dumps(
            {
                "last_run": last_run,
                "active_google": active_google,
                "active_caldav": active_caldav,
                "conflicts": conflicts,
                "error": error,
                "skipped": skipped,
                "skip_reason": skip_reason,
            }
        ),
    )


def get_power_devices(storage: Storage) -> list[PowerDevice]:
    """Device list for the power view; falls back to the defaults.

    An empty stored list is a deliberate choice ("no devices") and is
    returned as such — only a missing or unparseable value falls back.

    Entity ids are re-validated here even though the admin API already
    validates on write (defense in depth: the settings table is trusted
    input today, but a future write path or a manually edited DB should
    not be able to smuggle something odd into a request against HA).
    Individual entries failing the check are skipped and logged rather
    than failing the whole list.
    """
    raw = storage.get_setting(POWER_DEVICES_KEY)
    if raw is None:
        return list(DEFAULT_POWER_DEVICES)
    try:
        items = json.loads(raw)
        # name is optional (empty → use the HA friendly_name at display time).
        devices = [PowerDevice(item["entity_id"], item.get("name") or "") for item in items]
    except (ValueError, TypeError, KeyError):
        return list(DEFAULT_POWER_DEVICES)
    valid_devices = []
    for device in devices:
        if is_valid_power_entity_id(device.entity_id):
            valid_devices.append(device)
        else:
            logger.warning(
                "Skipping power device with invalid entity_id: %r", device.entity_id
            )
    return valid_devices


def set_power_devices(storage: Storage, devices: list[PowerDevice]) -> None:
    """Persist the power-view device list (validation happens in the API layer)."""
    storage.set_setting(
        POWER_DEVICES_KEY,
        json.dumps(
            [{"entity_id": device.entity_id, "name": device.name} for device in devices]
        ),
    )
