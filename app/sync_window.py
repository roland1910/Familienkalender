"""The fetch windows, one per source type.

Kept in its own module rather than in app.sync: app.sync imports the three
outgoing syncs, and app.birthday_sync needs the very same window definition —
importing app.sync there would be circular. One definition means the fetch,
the stale-row pruning in ``storage.sync_events`` and the birthday sync can
never disagree about which range a source actually covers.

Two windows exist:

- **Calendars** (``google``, ``caldav``): -7/+90 days. Enough for the month
  and week views, the ICS feed and both outgoing calendar syncs, and small
  enough to keep every fetch cheap.
- **Contact sources** (``google_contacts``): a bit over a *year*. Birthdays
  are the one source type where the narrow window is actively wrong: a
  contact whose birthday is five months away simply did not exist in the
  system, so the birthday sync could not write a series for them (it wrote
  21 of Roland's contacts, not all of them) and no one could page forward to
  see it. With a full year every contact carrying a birthday is present.
"""

from datetime import UTC, datetime, time, timedelta

from app.models import LOCAL_TZ

SYNC_WINDOW_PAST_DAYS = 7
SYNC_WINDOW_FUTURE_DAYS = 90

# Contact sources reach back the same 7 days (a birthday two days ago should
# still be visible) and forward 400.
#
# Why 400 and not 365: two consecutive occurrences of the same birthday can
# be 366 days apart (whenever 29 February lies between them), so a window of
# exactly a year could miss a person entirely — the very bug this window
# exists to fix. Anything > 366 covers everyone; 400 keeps a comfortable
# margin and is still a single, cheap People-API fetch.
#
# The window therefore spans MORE than a year, which means a person can have
# two occurrences inside it. app.sources.google_contacts emits only the
# earlier one, so there is still exactly one event per person.
CONTACTS_WINDOW_PAST_DAYS = 7
CONTACTS_WINDOW_FUTURE_DAYS = 400

# Source types fetched with the contact window.
CONTACT_SOURCE_TYPES = frozenset({"google_contacts"})


def _window(now: datetime | None, past_days: int, future_days: int) -> tuple[datetime, datetime]:
    """[local midnight - past_days, local midnight + future_days)."""
    now_local = (now or datetime.now(UTC)).astimezone(LOCAL_TZ)
    start_day = now_local.date() - timedelta(days=past_days)
    end_day = now_local.date() + timedelta(days=future_days)
    return (
        datetime.combine(start_day, time.min, tzinfo=LOCAL_TZ),
        datetime.combine(end_day, time.min, tzinfo=LOCAL_TZ),
    )


def sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The calendar window: local midnight 7 days back to 90 days ahead."""
    return _window(now, SYNC_WINDOW_PAST_DAYS, SYNC_WINDOW_FUTURE_DAYS)


def contacts_sync_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The contact window: local midnight 7 days back to 400 days ahead."""
    return _window(now, CONTACTS_WINDOW_PAST_DAYS, CONTACTS_WINDOW_FUTURE_DAYS)


def source_sync_window(
    source_type: str, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """The window one source is fetched AND pruned with.

    Both must be the same range: ``storage.sync_events`` deletes the stored
    events of that source inside the window that the fetch did not return, so
    a wider prune than fetch would drop exactly the events another run just
    created.
    """
    if source_type in CONTACT_SOURCE_TYPES:
        return contacts_sync_window(now)
    return sync_window(now)
