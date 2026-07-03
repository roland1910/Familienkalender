"""Central validation of calendar source URLs (SSRF protection).

Source URLs are entered in the admin UI, but the config could reach the
database by other means too — so validation runs both when a source is
saved (admin API) and defensively before every fetch (CalDAV client).

Rules:

- ``https`` only. ``http`` can be allowed via FAMILIENKALENDER_ALLOW_HTTP
  for local development and E2E stubs without TLS.
- No userinfo in the URL (credentials belong in the source config, where
  they never leak into logs or error messages).
- IP-literal hosts must not point into link-local or multicast ranges or
  the Home Assistant supervisor/add-on network 172.30.32.0/23 — a hostile
  source config must not be able to probe HA-internal services.

Hostnames are not resolved here: a DNS-based check would be bypassable
via rebinding anyway (TOCTOU between check and fetch) and would make
validation depend on the resolver. The IP-literal check blocks the
straightforward attacks; the HA-internal services themselves require
authentication.
"""

import ipaddress
import os
from urllib.parse import urlsplit

# The Home Assistant supervisor/add-on internal network.
HA_INTERNAL_NETWORK = ipaddress.ip_network("172.30.32.0/23")


class SourceURLError(ValueError):
    """A source URL failed validation. Messages are German (admin UI)."""


def _http_allowed() -> bool:
    return bool(os.environ.get("FAMILIENKALENDER_ALLOW_HTTP"))


def _check_ip_literal(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not an IP literal
    if (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or (address.version == 4 and address in HA_INTERNAL_NETWORK)
    ):
        raise SourceURLError(f"Ziel-Adresse {host} ist nicht erlaubt (internes Netz)")


def validate_source_url(url: str) -> str:
    """Validate a source URL; returns it unchanged or raises SourceURLError."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise SourceURLError(f"Ungültige URL: {url!r}") from exc
    allowed_schemes = ("https", "http") if _http_allowed() else ("https",)
    if parts.scheme not in allowed_schemes:
        raise SourceURLError("Nur https-URLs sind erlaubt")
    if parts.username is not None or parts.password is not None:
        raise SourceURLError("Zugangsdaten gehören nicht in die URL")
    if not parts.hostname:
        raise SourceURLError("Die URL enthält keinen Hostnamen")
    _check_ip_literal(parts.hostname)
    return url
