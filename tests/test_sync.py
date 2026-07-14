"""Tests for the sync orchestration (fetch all sources, isolate errors)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.models import AuditEntry, CalendarEvent
from app.sources import limits
from app.storage import Storage
from app.sync import (
    SYNC_LOCK,
    SYNC_WINDOW_FUTURE_DAYS,
    SYNC_WINDOW_PAST_DAYS,
    sync_all,
    sync_window,
)

BERLIN = ZoneInfo("Europe/Berlin")
FIXED_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


def make_event(uid: str = "uid-1") -> CalendarEvent:
    return CalendarEvent(
        uid=uid,
        title="Termin",
        start=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
        end=datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
        all_day=False,
    )


@pytest.mark.anyio
class TestTextLimits:
    async def test_title_and_location_are_truncated_on_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(
            type="caldav", name="Firma", config={"calendar_url": "https://x/cal/"}
        )
        hostile = CalendarEvent(
            uid="uid-long",
            title="T" * 5000,
            start=datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
            end=datetime(2026, 7, 10, 19, 0, tzinfo=UTC),
            all_day=False,
            location="L" * 5000,
        )

        async def fake_fetch(config, window_start, window_end, *, client=None):
            return [hostile]

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        await sync_all(storage, now=FIXED_NOW)

        stored = storage.get_events(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
        )
        assert limits.MAX_TEXT_LENGTH == 1000
        assert stored[0].event.title == "T" * limits.MAX_TEXT_LENGTH
        assert stored[0].event.location == "L" * limits.MAX_TEXT_LENGTH

    async def test_short_texts_and_missing_location_stay_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(
            type="caldav", name="Firma", config={"calendar_url": "https://x/cal/"}
        )

        async def fake_fetch(config, window_start, window_end, *, client=None):
            return [make_event()]

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        await sync_all(storage, now=FIXED_NOW)

        stored = storage.get_events(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)
        )
        assert stored[0].event.title == "Termin"
        assert stored[0].event.location is None


class TestSyncWindow:
    def test_window_spans_minus_7_to_plus_90_days(self) -> None:
        now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
        window_start, window_end = sync_window(now)
        assert window_start == datetime(2026, 6, 26, 0, 0, tzinfo=BERLIN)
        assert window_end == datetime(2026, 10, 1, 0, 0, tzinfo=BERLIN)
        assert SYNC_WINDOW_PAST_DAYS == 7
        assert SYNC_WINDOW_FUTURE_DAYS == 90

    def test_window_uses_local_calendar_days(self) -> None:
        # 23:30 UTC on July 3rd is already July 4th in Berlin.
        now = datetime(2026, 7, 3, 23, 30, tzinfo=UTC)
        window_start, _ = sync_window(now)
        assert window_start == datetime(2026, 6, 27, 0, 0, tzinfo=BERLIN)


@pytest.mark.anyio
class TestSyncAll:
    async def test_caldav_source_is_fetched_and_stored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        source_id = storage.add_source(
            type="caldav", name="Firma", config={"calendar_url": "https://x/cal/"}
        )
        seen: dict = {}

        async def fake_fetch(config, window_start, window_end, *, client=None):
            seen["config"] = config
            seen["window"] = (window_start, window_end)
            return [make_event()]

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)

        results = await sync_all(storage, now=FIXED_NOW)

        assert results == {source_id: None}
        assert seen["config"] == {"calendar_url": "https://x/cal/"}
        events = storage.get_events(*seen["window"])
        assert [item.event.uid for item in events] == ["uid-1"]
        assert storage.list_sources()[0].last_sync_at is not None
        assert storage.list_sources()[0].last_sync_error is None

    async def test_google_source_uses_token_file_for_its_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        storage = Storage(tmp_path / "test.db")
        source_id = storage.add_source(
            type="google", name="Marina", config={"calendar_id": "m@example.com"}
        )
        seen: dict = {}

        async def fake_fetch(config, window_start, window_end, *, token_file, client=None):
            seen["token_file"] = token_file
            return []

        monkeypatch.setattr("app.sources.google.fetch_events", fake_fetch)

        results = await sync_all(storage, now=FIXED_NOW)

        assert results == {source_id: None}
        assert seen["token_file"] == tmp_path / f"google_token_{source_id}.json"

    async def test_google_contacts_source_uses_people_client_and_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        storage = Storage(tmp_path / "test.db")
        source_id = storage.add_source(
            type="google_contacts", name="Geburtstage", config={}
        )
        seen: dict = {}

        async def fake_contacts(config, window_start, window_end, *, token_file, client=None):
            seen["token_file"] = token_file
            return [
                CalendarEvent(
                    uid="people/c1|2026",
                    title="🎂 Oma",
                    start=datetime(2026, 7, 10, tzinfo=UTC).date(),
                    end=datetime(2026, 7, 11, tzinfo=UTC).date(),
                    all_day=True,
                )
            ]

        # The calendar client must NOT be used for this source type.
        async def fail_calendar(*args, **kwargs):
            raise AssertionError("google_contacts must not use the Calendar client")

        monkeypatch.setattr("app.sources.google_contacts.fetch_events", fake_contacts)
        monkeypatch.setattr("app.sources.google.fetch_events", fail_calendar)

        results = await sync_all(storage, now=FIXED_NOW)

        assert results == {source_id: None}
        assert seen["token_file"] == tmp_path / f"google_token_{source_id}.json"
        events = storage.get_events(*sync_window(FIXED_NOW))
        assert [item.event.uid for item in events] == ["people/c1|2026"]

    async def test_disabled_sources_are_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={}, enabled=False)

        async def fail_fetch(*args, **kwargs):
            raise AssertionError("disabled source must not be fetched")

        monkeypatch.setattr("app.sources.caldav.fetch_events", fail_fetch)

        assert await sync_all(storage, now=FIXED_NOW) == {}

    async def test_one_broken_source_does_not_block_the_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        broken_id = storage.add_source(type="caldav", name="Kaputt", config={})
        ok_id = storage.add_source(
            type="google", name="Marina", config={"calendar_id": "m@example.com"}
        )

        async def broken_fetch(*args, **kwargs):
            raise RuntimeError("Server unreachable")

        async def ok_fetch(config, window_start, window_end, *, token_file, client=None):
            return [make_event(uid="from-google")]

        monkeypatch.setattr("app.sources.caldav.fetch_events", broken_fetch)
        monkeypatch.setattr("app.sources.google.fetch_events", ok_fetch)

        results = await sync_all(storage, now=FIXED_NOW)

        assert results[broken_id] == "Server unreachable"
        assert results[ok_id] is None
        sources = {source.id: source for source in storage.list_sources()}
        assert sources[broken_id].last_sync_error == "Server unreachable"
        assert sources[ok_id].last_sync_error is None
        # The healthy source's events were stored despite the broken one.
        window = sync_window(FIXED_NOW)
        assert [item.event.uid for item in storage.get_events(*window)] == ["from-google"]

    async def test_network_timeout_is_isolated_like_any_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real httpx exception type: error isolation must also hold for
        # network-level failures, not only for plain RuntimeErrors.
        storage = Storage(tmp_path / "test.db")
        timeout_id = storage.add_source(type="caldav", name="Firma", config={})
        ok_id = storage.add_source(
            type="google", name="Marina", config={"calendar_id": "m@example.com"}
        )

        async def timeout_fetch(*args, **kwargs):
            raise httpx.ConnectTimeout("Connection to cloud.example.com timed out")

        async def ok_fetch(config, window_start, window_end, *, token_file, client=None):
            return [make_event(uid="from-google")]

        monkeypatch.setattr("app.sources.caldav.fetch_events", timeout_fetch)
        monkeypatch.setattr("app.sources.google.fetch_events", ok_fetch)

        results = await sync_all(storage, now=FIXED_NOW)

        assert "timed out" in (results[timeout_id] or "")
        assert results[ok_id] is None
        sources = {source.id: source for source in storage.list_sources()}
        assert "timed out" in (sources[timeout_id].last_sync_error or "")
        window = sync_window(FIXED_NOW)
        assert [item.event.uid for item in storage.get_events(*window)] == ["from-google"]

    async def test_successful_sync_clears_previous_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        source_id = storage.add_source(type="caldav", name="Firma", config={})
        storage.update_sync_status(
            source_id, synced_at=datetime.now(UTC), error="alter Fehler"
        )

        async def ok_fetch(config, window_start, window_end, *, client=None):
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", ok_fetch)

        await sync_all(storage, now=FIXED_NOW)

        assert storage.list_sources()[0].last_sync_error is None


@pytest.mark.anyio
class TestSyncTimestamp:
    async def test_all_sources_share_one_timestamp_per_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})
        storage.add_source(type="caldav", name="Zweite", config={})

        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(0.01)
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", slow_fetch)

        # No fixed `now`: the timestamp must still be taken once per run,
        # not once per source.
        await sync_all(storage)

        first, second = storage.list_sources()
        assert first.last_sync_at == second.last_sync_at


@pytest.mark.anyio
class TestErrorSanitizing:
    async def test_credentials_in_error_reach_neither_db_nor_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})

        async def leaking_fetch(*args, **kwargs):
            raise RuntimeError(
                "REPORT https://roland:super-geheim@cloud.example.com/dav/ failed"
            )

        monkeypatch.setattr("app.sources.caldav.fetch_events", leaking_fetch)

        with caplog.at_level("WARNING", logger="app.sync"):
            results = await sync_all(storage, now=FIXED_NOW)

        stored_error = storage.list_sources()[0].last_sync_error
        assert stored_error is not None
        assert "super-geheim" not in stored_error
        assert "cloud.example.com" in stored_error
        assert all("super-geheim" not in r.getMessage() for r in caplog.records)
        assert all("super-geheim" not in error for error in results.values() if error)


@pytest.mark.anyio
class TestSyncLock:
    async def test_sync_all_holds_the_module_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})
        seen: dict = {}

        async def observing_fetch(*args, **kwargs):
            seen["locked_during_fetch"] = SYNC_LOCK.locked()
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", observing_fetch)

        await sync_all(storage, now=FIXED_NOW)

        assert seen["locked_during_fetch"] is True
        assert SYNC_LOCK.locked() is False

    async def test_concurrent_sync_runs_serialize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})
        active = 0
        max_active = 0

        async def slow_fetch(*args, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", slow_fetch)

        await asyncio.gather(
            sync_all(storage, now=FIXED_NOW),
            sync_all(storage, now=FIXED_NOW),
        )

        assert max_active == 1


@pytest.mark.anyio
class TestIncomingChangeLog:
    async def test_added_events_are_logged_incoming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})

        async def fake_fetch(*args, **kwargs):
            return [make_event(uid="new-1")]

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        await sync_all(storage, now=FIXED_NOW)

        entries = storage.get_audit_entries("2026-01-01T00:00:00+00:00")
        assert len(entries) == 1
        assert entries[0].direction == "in"
        assert entries[0].scope == "Firma"
        assert entries[0].action == "added"
        assert entries[0].title == "Termin"

    async def test_unchanged_second_sync_logs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})

        async def fake_fetch(*args, **kwargs):
            return [make_event(uid="stable")]

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        await sync_all(storage, now=FIXED_NOW)
        # Second run with identical data: no new change-log entries.
        await sync_all(storage, now=FIXED_NOW)

        entries = storage.get_audit_entries("2026-01-01T00:00:00+00:00")
        assert len(entries) == 1

    async def test_prune_removes_entries_older_than_retention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})
        # An old entry, well before the retention window from FIXED_NOW.
        storage.add_audit_entries(
            [
                AuditEntry(
                    ts="2026-01-01T00:00:00+00:00",
                    direction="in",
                    scope="Firma",
                    action="added",
                    title="alt",
                )
            ]
        )

        async def fake_fetch(*args, **kwargs):
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        await sync_all(storage, now=FIXED_NOW)

        entries = storage.get_audit_entries("2020-01-01T00:00:00+00:00")
        assert entries == []


@pytest.mark.anyio
class TestBusySyncIntegration:
    async def test_busy_sync_runs_after_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})
        called = {}

        async def fake_busy(storage_arg, *, now=None, client=None):
            from app.busy_sync import BusySyncResult

            called["ran"] = True
            called["now"] = now
            return BusySyncResult(0, 0, 0, 0, 0, None)

        async def fake_fetch(*args, **kwargs):
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        monkeypatch.setattr("app.busy_sync.run_busy_sync", fake_busy)
        await sync_all(storage, now=FIXED_NOW)
        assert called["ran"] is True
        assert called["now"] == FIXED_NOW

    async def test_busy_sync_error_does_not_break_calendar_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = Storage(tmp_path / "test.db")
        storage.add_source(type="caldav", name="Firma", config={})

        async def boom(*args, **kwargs):
            raise RuntimeError("busy sync exploded")

        async def fake_fetch(*args, **kwargs):
            return []

        monkeypatch.setattr("app.sources.caldav.fetch_events", fake_fetch)
        monkeypatch.setattr("app.busy_sync.run_busy_sync", boom)
        # Must not raise: busy-sync errors are isolated from the calendar sync.
        results = await sync_all(storage, now=FIXED_NOW)
        assert results  # calendar sources still processed
