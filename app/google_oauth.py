"""Interactive Google OAuth flow for the admin UI (copy-paste variant).

The add-on runs headless behind HA ingress, so the classic loopback
redirect (a local callback server) is not reachable from the user's
browser. Instead the desktop-app client flow is used with an
intentionally unreachable redirect URI (http://localhost:1/): the user
authorizes in any browser, the redirect fails visibly, and the full
redirect URL (or just its ``code`` parameter) is pasted into the admin
UI. The backend exchanges the code for tokens; app.sources.google then
consumes and refreshes them.

All error messages raised here are German — they surface directly in
the admin UI.

Deliberately no ``state`` parameter and no PKCE: both protect a
redirect-based callback against CSRF/code injection, but in this
copy-paste flow there is no callback the attacker could steer — the
admin manually carries the code from the browser to the (ingress- and
IP-allowlist-protected) admin UI. What the parked tokens needed instead
is a binding between "code eingelöst" and "Quelle angelegt": that is
covered by the random per-flow id from POST /google/connect, which
create_source must present to adopt the tokens.
"""

import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx

AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

# Port 1 is never served, so the redirect always fails visibly and the
# user copies the URL from the browser's address bar (see module docstring).
REDIRECT_URI = "http://localhost:1/"

# calendarList pages at most a handful of times for a personal account;
# more pages mean a broken or hostile response keeping us in the loop.
_MAX_CALENDAR_LIST_PAGES = 10


class GoogleOAuthError(Exception):
    """OAuth flow failure with a German, admin-UI-ready message."""


def build_auth_url(client_id: str) -> str:
    """The consent URL the user opens in any browser."""
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent forces Google to issue a refresh token even if
        # the account already authorized this app before.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_BASE_URL}?{urlencode(params)}"


def _query_of_pasted_url(text: str) -> str | None:
    """The query string if the paste looks like a URL/query, else None.

    URL detection goes by structure (``://`` or ``?``, or a leading ``?``
    / bare ``code=...`` fragment) instead of searching for the substring
    ``code=`` anywhere — a code parameter hidden inside another parameter's
    value must not be mistaken for the real one, and a URL without any
    query must still be recognized as a URL.
    """
    if "://" in text or "?" in text:
        # urlsplit also handles a bare "?code=..." query string.
        return urlsplit(text).query
    if text.startswith("code=") or "&" in text:
        return text
    return None


def extract_auth_code(raw: str) -> str:
    """Extract the authorization code from whatever the user pasted.

    Accepts the full redirect URL (with or without scheme), just its
    query string, a ``code=...`` fragment, or the raw (possibly still
    percent-encoded) code itself.
    """
    text = raw.strip()
    if not text:
        raise GoogleOAuthError("Bitte die Weiterleitungs-URL oder den Code einfügen.")
    query = _query_of_pasted_url(text)
    if query is not None:
        codes = parse_qs(query).get("code")
        if codes and codes[0].strip():
            return codes[0].strip()
        raise GoogleOAuthError(
            "In der eingefügten URL wurde kein Code gefunden — bitte die"
            " vollständige Weiterleitungs-URL kopieren."
        )
    # Raw code. Browsers percent-encode the slash in Google codes (4%2F...).
    return unquote(text) if "%" in text else text


def _exchange_error(response: httpx.Response) -> GoogleOAuthError:
    """Map a token-endpoint error response to a German message.

    The response body is never quoted verbatim: it belongs to an exchange
    that involved the client secret and could echo request data.
    """
    error_code = ""
    with contextlib.suppress(json.JSONDecodeError, AttributeError):
        error_code = response.json().get("error", "")
    if error_code == "invalid_grant":
        return GoogleOAuthError(
            "Der Code ist ungültig oder abgelaufen. Bitte den Vorgang neu"
            " starten und den frischen Code direkt einfügen."
        )
    if error_code in ("invalid_client", "unauthorized_client"):
        return GoogleOAuthError(
            "Google lehnt die App-Zugangsdaten ab — bitte Client-ID und"
            " Client-Secret in den Einstellungen prüfen."
        )
    detail = f" ({error_code})" if error_code else ""
    return GoogleOAuthError(
        f"Google-Anmeldung fehlgeschlagen: HTTP {response.status_code}{detail}"
    )


async def exchange_code(
    code: str,
    *,
    client_id: str,
    client_secret: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens.

    Returns a dict in the shape app.sources.google persists and consumes
    (client credentials included for later refresh grants).
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await exchange_code(
                code, client_id=client_id, client_secret=client_secret, client=own_client
            )
    response = await client.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code != 200:
        raise _exchange_error(response)
    payload = response.json()
    if not payload.get("refresh_token"):
        raise GoogleOAuthError(
            "Google hat kein Refresh-Token geliefert. Bitte den Vorgang neu"
            " starten und beim Anmelden alle Berechtigungen bestätigen."
        )
    expires_at = datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": payload["refresh_token"],
        "access_token": payload["access_token"],
        "access_token_expires_at": expires_at.isoformat(),
    }


async def fetch_calendar_list(
    access_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, str]]:
    """The account's calendars (id and display name) via calendarList.list."""
    if client is None:
        async with httpx.AsyncClient(timeout=30) as own_client:
            return await fetch_calendar_list(access_token, client=own_client)
    calendars: list[dict[str, str]] = []
    page_token: str | None = None
    for _ in range(_MAX_CALENDAR_LIST_PAGES):
        params = {"maxResults": "250"}
        if page_token:
            params["pageToken"] = page_token
        response = await client.get(
            CALENDAR_LIST_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code != 200:
            raise GoogleOAuthError(
                f"Kalenderliste konnte nicht geladen werden (HTTP {response.status_code})."
            )
        payload = response.json()
        calendars.extend(
            {"id": item["id"], "name": item.get("summary", item["id"])}
            for item in payload.get("items", [])
            if item.get("id")
        )
        page_token = payload.get("nextPageToken")
        if not page_token:
            return calendars
    raise GoogleOAuthError("Kalenderliste konnte nicht geladen werden (zu viele Seiten).")
