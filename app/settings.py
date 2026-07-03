"""Typed access to persisted admin settings.

Settings live in the SQLite ``settings`` table (key/value, see
app.storage). This module owns the known keys and the parsing/fallback
logic so API and sync code never deal with raw strings.
"""

import os
from datetime import time

from app.filtering import DEFAULT_EVENING_BOUNDARY
from app.storage import Storage

EVENING_BOUNDARY_KEY = "evening_boundary"
# Google OAuth app credentials (Desktop client). The client secret is a
# secret: it must never be returned by any GET API (see app.admin).
GOOGLE_CLIENT_ID_KEY = "google_client_id"
GOOGLE_CLIENT_SECRET_KEY = "google_client_secret"


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
