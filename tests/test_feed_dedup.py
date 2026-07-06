"""Tests for the ICS feed's cross-source duplicate collapsing.

Roland has the same appointment in several calendars (e.g. a meeting both
in his Google work calendar and in the Nextcloud one). In the subscribable
feed such duplicates must appear only once. Two events are duplicates when
their normalized title AND start AND end AND all_day flag match; the winner
is the one from the source with the higher feed_priority (ties broken by
the lower source id). This only affects the feed — the calendar views keep
showing both colored chips.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.feed import _moment_key, dedupe_feed_events, normalize_title
from app.models import CalendarEvent, StoredEvent

BERLIN = ZoneInfo("Europe/Berlin")


def _stored(
    source_id: int,
    title: str,
    start: datetime | date,
    end: datetime | date,
    *,
    all_day: bool = False,
    feed_priority: int = 0,
    uid: str = "uid",
) -> StoredEvent:
    return StoredEvent(
        source_id=source_id,
        source_name=f"src-{source_id}",
        display_mode="filtered",
        event=CalendarEvent(
            uid=uid, title=title, start=start, end=end, all_day=all_day
        ),
        feed_priority=feed_priority,
    )


def _titles(items: list[StoredEvent]) -> set[str]:
    return {item.event.title for item in items}


def _source_ids(items: list[StoredEvent]) -> set[int]:
    return {item.source_id for item in items}


class TestNormalizeTitle:
    def test_lowercases(self) -> None:
        assert normalize_title("Meeting") == normalize_title("meeting")

    def test_trims_leading_and_trailing_whitespace(self) -> None:
        assert normalize_title("  Meeting  ") == normalize_title("Meeting")

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_title("Team   Sync") == normalize_title("Team Sync")

    def test_mixed(self) -> None:
        assert normalize_title("  TEAM\t Sync \n") == "team sync"


class TestDedupeFeedEvents:
    def test_collapses_identical_event_across_two_sources(self) -> None:
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting", start, end, feed_priority=0),
                _stored(2, "Meeting", start, end, feed_priority=5),
            ]
        )
        assert len(result) == 1
        # Higher priority (source 2) wins.
        assert _source_ids(result) == {2}

    def test_tie_break_by_lower_source_id(self) -> None:
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(5, "Meeting", start, end, feed_priority=3),
                _stored(2, "Meeting", start, end, feed_priority=3),
            ]
        )
        assert len(result) == 1
        assert _source_ids(result) == {2}

    def test_default_priority_still_collapses_via_source_id(self) -> None:
        # Both at the default priority 0 → the duplicate is still removed,
        # the lower source id wins.
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(9, "Meeting", start, end),
                _stored(3, "Meeting", start, end),
            ]
        )
        assert _source_ids(result) == {3}

    def test_different_title_is_not_a_duplicate(self) -> None:
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting A", start, end),
                _stored(2, "Meeting B", start, end),
            ]
        )
        assert len(result) == 2
        assert _titles(result) == {"Meeting A", "Meeting B"}

    def test_different_start_is_not_a_duplicate(self) -> None:
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting", datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN), end),
                _stored(2, "Meeting", datetime(2026, 7, 10, 15, 0, tzinfo=BERLIN), end),
            ]
        )
        assert len(result) == 2

    def test_different_end_is_not_a_duplicate(self) -> None:
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting", start, datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)),
                _stored(2, "Meeting", start, datetime(2026, 7, 10, 18, 0, tzinfo=BERLIN)),
            ]
        )
        assert len(result) == 2

    def test_same_instant_in_different_zones_is_a_duplicate(self) -> None:
        # 16:00 Berlin == 14:00 UTC — same instant, must collapse.
        start_berlin = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end_berlin = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        start_utc = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
        end_utc = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting", start_berlin, end_berlin),
                _stored(2, "Meeting", start_utc, end_utc),
            ]
        )
        assert len(result) == 1

    def test_all_day_and_timed_do_not_merge(self) -> None:
        # An all-day event and a timed event on the same day are distinct.
        result = dedupe_feed_events(
            [
                _stored(
                    1,
                    "Geburtstag",
                    date(2026, 7, 10),
                    date(2026, 7, 11),
                    all_day=True,
                ),
                _stored(
                    2,
                    "Geburtstag",
                    datetime(2026, 7, 10, 0, 0, tzinfo=BERLIN),
                    datetime(2026, 7, 11, 0, 0, tzinfo=BERLIN),
                    all_day=False,
                ),
            ]
        )
        assert len(result) == 2

    def test_all_day_duplicate_across_contact_sources_collapses(self) -> None:
        # The same birthday from two contact sources (all-day, same date).
        result = dedupe_feed_events(
            [
                _stored(
                    1,
                    "Oma",
                    date(2026, 7, 10),
                    date(2026, 7, 11),
                    all_day=True,
                    feed_priority=0,
                ),
                _stored(
                    2,
                    "Oma",
                    date(2026, 7, 10),
                    date(2026, 7, 11),
                    all_day=True,
                    feed_priority=10,
                ),
            ]
        )
        assert len(result) == 1
        assert _source_ids(result) == {2}

    def test_title_normalization_collapses_case_and_whitespace(self) -> None:
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Team  Sync", start, end),
                _stored(2, "  team sync ", start, end, feed_priority=1),
            ]
        )
        assert len(result) == 1
        assert _source_ids(result) == {2}

    def test_preserves_the_winner_object_unchanged(self) -> None:
        # The surviving StoredEvent (and its stable-UID identity) is the
        # original object, so feed UID stability is preserved.
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        winner = _stored(2, "Meeting", start, end, feed_priority=5, uid="keep-me")
        result = dedupe_feed_events(
            [_stored(1, "Meeting", start, end, uid="drop-me"), winner]
        )
        assert result == [winner]

    def test_empty_input(self) -> None:
        assert dedupe_feed_events([]) == []

    def test_non_duplicates_keep_their_order(self) -> None:
        # Distinct events pass through in input order (deterministic feed).
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        a = _stored(1, "Alpha", start, end)
        b = _stored(2, "Beta", start, end)
        c = _stored(3, "Gamma", start, end)
        assert dedupe_feed_events([a, b, c]) == [a, b, c]

    def test_same_source_same_key_both_survive(self) -> None:
        # Two contacts "Oma" and "Müller" that happen to share a birthday key
        # come from the SAME contacts source (same source_id, different uid).
        # They are genuinely different appointments — both must survive.
        result = dedupe_feed_events(
            [
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, uid="contact-oma",
                ),
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, uid="contact-mueller",
                ),
            ]
        )
        assert len(result) == 2
        assert {item.event.uid for item in result} == {
            "contact-oma",
            "contact-mueller",
        }

    def test_mixed_winning_source_keeps_all_its_events(self) -> None:
        # Source A (higher priority) has two same-key "Oma" birthdays; source B
        # (lower priority) has one. The winning source A keeps BOTH of its real
        # appointments; B's contribution loses. No real A appointment is ever
        # replaced by a single B event.
        result = dedupe_feed_events(
            [
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=5, uid="a-1",
                ),
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=5, uid="a-2",
                ),
                _stored(
                    2, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=0, uid="b-1",
                ),
            ]
        )
        assert {item.event.uid for item in result} == {"a-1", "a-2"}
        assert _source_ids(result) == {1}

    def test_mixed_losing_source_still_loses_all_its_events(self) -> None:
        # Symmetric case: now source B wins. A (two same-key events) loses
        # completely; only B's single event survives. This is the deliberate
        # trade-off — cross-source precedence collapses to the winning source.
        result = dedupe_feed_events(
            [
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=0, uid="a-1",
                ),
                _stored(
                    1, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=0, uid="a-2",
                ),
                _stored(
                    2, "Oma", date(2026, 7, 10), date(2026, 7, 11),
                    all_day=True, feed_priority=9, uid="b-1",
                ),
            ]
        )
        assert {item.event.uid for item in result} == {"b-1"}
        assert _source_ids(result) == {2}

    def test_same_source_duplicates_survive_even_with_cross_source_loser(
        self,
    ) -> None:
        # Winning source has duplicates AND there is a lower-priority other
        # source: all winning-source events survive, the other source drops.
        start = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        end = datetime(2026, 7, 10, 17, 0, tzinfo=BERLIN)
        result = dedupe_feed_events(
            [
                _stored(1, "Meeting", start, end, feed_priority=3, uid="a-1"),
                _stored(1, "Meeting", start, end, feed_priority=3, uid="a-2"),
                _stored(2, "Meeting", start, end, feed_priority=1, uid="b-1"),
            ]
        )
        assert {item.event.uid for item in result} == {"a-1", "a-2"}


class TestMomentKey:
    def test_naive_datetime_uses_local_zone_not_process_zone(self) -> None:
        # A tz-less datetime must be interpreted in Europe/Berlin, not the
        # process timezone, so its instant matches the aware Berlin form.
        naive = datetime(2026, 7, 10, 16, 0)
        aware_berlin = datetime(2026, 7, 10, 16, 0, tzinfo=BERLIN)
        assert _moment_key(naive) == _moment_key(aware_berlin)
