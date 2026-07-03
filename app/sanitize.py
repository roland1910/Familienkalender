"""Sanitizing of error messages before they are stored or logged.

Exception texts from HTTP clients routinely embed the request URL — which
for CalDAV carries basic-auth credentials (https://user:password@host/).
Every message that ends up in last_sync_error or the logs must pass
through sanitize_error first.
"""

import re

# userinfo part of a URL: everything between "://" and "@" (no slash or
# whitespace in between, so ordinary prose around URLs is left alone).
_URL_USERINFO = re.compile(r"://[^/@\s]*@")

# Error messages are for the admin UI and logs, not full tracebacks;
# 500 characters keep pathological messages from bloating DB and log.
MAX_ERROR_LENGTH = 500


def sanitize_error(text: str) -> str:
    """Strip credentials from URLs in an error text and cap its length."""
    cleaned = _URL_USERINFO.sub("://", text)
    return cleaned[:MAX_ERROR_LENGTH]
