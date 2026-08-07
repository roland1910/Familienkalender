"""Shared event identity and window selection for the two outgoing syncs.

Both write directions — the "Busy MV" sync (MoreValue → Xalt, app.busy_sync)
and the mirror sync (Xalt → MoreValue, app.mirror_sync) — key their mapping
tables by the SAME identity the events table uses. Keeping that definition in
one place means a change to the identity can never leave the two syncs
disagreeing about which stored event a persisted mapping row belongs to.
"""

from datetime import datetime

from app.models import StoredEvent
from app.storage import Storage, _encode_moment


def source_key(source_id: int, event) -> str:
    """Stable key for a source event, matching the events-table identity.

    ``source_id|uid|<encoded-start>`` — the encoded start uses the same
    encoding storage uses (UTC ISO for timed, ISO date for all-day), so the
    key is stable across syncs and independent of the process timezone.
    """
    return f"{source_id}|{event.uid}|{_encode_moment(event.start)}"


def desired_source_events(
    storage: Storage,
    source_ids: set[int],
    window_start: datetime,
    window_end: datetime,
) -> dict[str, StoredEvent]:
    """Events of the selected sources to write out, keyed by source key.

    Only events whose START lies within [window_start, window_end) — all-day
    events included, past events excluded. No family filtering happens here:
    the evening/weekend rule governs what is DISPLAYED, while both outgoing
    syncs deliberately carry every appointment of the selected calendars
    (a plain 10:15 meeting has to reach the other calendar too).
    """
    desired: dict[str, StoredEvent] = {}
    for item in storage.get_events(window_start, window_end):
        if item.source_id not in source_ids:
            continue
        start = item.event.start_as_datetime()
        # storage.get_events returns everything OVERLAPPING the window,
        # including an event that started before window_start and merely
        # reaches into it. Only a start actually inside the window counts
        # here — nothing is ever written into the past.
        if not (window_start <= start < window_end):
            continue
        desired[source_key(item.source_id, item.event)] = item
    return desired
