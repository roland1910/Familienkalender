"""Deleting a file from the media share through Home Assistant.

The add-on cannot delete a photo itself, and that is deliberate: ``/media``
is mounted read-only (config.yaml ``map: media:ro``) and AppArmor denies
every write below it (apparmor.txt ``deny /media/** wklx``). Nothing in this
feature loosens that — the deletion is delegated to Home Assistant, which
owns the share and already exposes exactly this operation:

    ws://supervisor/core/websocket
    -> {"type": "auth", "access_token": <SUPERVISOR_TOKEN>}
    -> {"id": N, "type": "media_source/local_source/remove",
        "media_content_id": "media-source://media_source/local/<relpath>"}
    <- {"id": N, "success": true}

The WebSocket handshake is the same one ``app.auth`` speaks for the admin
lookup, and the URL/token rules are shared with it (``ha_ws_url`` /
``ha_ws_token``) so there is one place that decides what may be sent where.

Security: the caller passes a path RELATIVE to the media root, derived from
the stored index entry — never from a client request. This module adds the
second half of that guarantee: ``media_content_id`` refuses anything that
is not a plain relative path (absolute, ``..``/``.`` segments, control
characters), so even a corrupted index row cannot be turned into an
identifier that addresses a different file.
"""

import asyncio
import json
import logging
import re

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from app.auth import HaConnectionError, ha_ws_token, ha_ws_url

logger = logging.getLogger(__name__)

# Identifier prefix of Home Assistant's local media source. "local" is the
# source directory id HA assigns to the /media mount (its media_dirs entry);
# everything after it is the path relative to that directory.
MEDIA_SOURCE_PREFIX = "media-source://media_source/local/"

# The WS command that unlinks a file from a local media source.
REMOVE_COMMAND = "media_source/local_source/remove"

# Generous: the file lives on a CIFS share and HA does the unlink itself.
DELETE_TIMEOUT_SECONDS = 20.0

# Longest relative path accepted into an identifier (bounded input).
MAX_RELATIVE_PATH_LENGTH = 1024

# Control characters have no business in a path and would break HA's URI
# regex (which is not DOTALL and anchors on $) in confusing ways.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class MediaDeleteError(Exception):
    """The file could not be deleted. The message is German and user-facing."""


def media_content_id(relative_path: str) -> str:
    """The media-source identifier for a path relative to the media root.

    THE PATH GOES IN VERBATIM — it must NOT be percent-encoded. Home
    Assistant matches the URI with a plain regex and then uses the part
    after the source id as a filesystem path without any unquoting (see
    ``URI_SCHEME_REGEX`` in media_source/const.py and
    ``async_parse_identifier`` in media_source/local_source.py). Encoding
    would therefore look for a file literally named ``%23recycle`` or
    ``Gr%C3%BCne%20Woche`` and fail — and the real share is full of spaces,
    umlauts and a ``#recycle`` folder. This is the kind of thing that looks
    like a missing ``quote()`` to a later reader, hence the paragraph.

    Separators are normalized to ``/`` so a path assembled on a Windows dev
    machine produces the identifier the Linux container would. Anything that
    is not a plain relative path raises MediaDeleteError.
    """
    path = (relative_path or "").replace("\\", "/")
    if not path or len(path) > MAX_RELATIVE_PATH_LENGTH:
        raise MediaDeleteError("Ungültiger Dateipfad.")
    if path.startswith("/") or _CONTROL_CHARS.search(path):
        raise MediaDeleteError("Ungültiger Dateipfad.")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise MediaDeleteError("Ungültiger Dateipfad.")
    return MEDIA_SOURCE_PREFIX + path


async def delete_media(relative_path: str) -> None:
    """Delete the file below the media root; raises MediaDeleteError.

    Returns normally only when Home Assistant confirmed ``success: true`` —
    the caller must not touch its own bookkeeping before that, or a photo
    would vanish from the list while still lying on the NAS.

    Every failure (no token, connection refused, timeout, rejected auth,
    unsuccessful command, malformed answer) becomes a MediaDeleteError with
    a German message. HA's own error text is logged, not shown: it is
    foreign text, and only its short code is carried into the message.
    """
    content_id = media_content_id(relative_path)
    try:
        token = ha_ws_token()
    except HaConnectionError as exc:
        raise MediaDeleteError(f"Home Assistant nicht erreichbar: {exc}") from exc
    try:
        async with asyncio.timeout(DELETE_TIMEOUT_SECONDS):
            async with connect(ha_ws_url()) as websocket:
                message = json.loads(await websocket.recv())
                if message.get("type") != "auth_required":
                    raise MediaDeleteError(
                        "Unerwartete Antwort von Home Assistant."
                    )
                await websocket.send(
                    json.dumps({"type": "auth", "access_token": token})
                )
                message = json.loads(await websocket.recv())
                if message.get("type") != "auth_ok":
                    raise MediaDeleteError(
                        "Home Assistant hat die Anmeldung abgelehnt."
                    )
                await websocket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "type": REMOVE_COMMAND,
                            "media_content_id": content_id,
                        }
                    )
                )
                while True:
                    message = json.loads(await websocket.recv())
                    if message.get("id") == 1 and message.get("type") == "result":
                        break
    except MediaDeleteError:
        raise
    except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
        raise MediaDeleteError(
            f"Löschen fehlgeschlagen: Home Assistant nicht erreichbar ({exc})."
        ) from exc
    if not message.get("success"):
        error = message.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        logger.warning("Home Assistant refused to delete a photo: %s", error)
        suffix = f" (Code: {code})" if isinstance(code, str) else ""
        raise MediaDeleteError(
            f"Home Assistant konnte die Datei nicht löschen{suffix}."
        )
