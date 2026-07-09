"""Tests for the one-way Busy MV sync (MoreValue -> Xalt primary calendar).

These cover the diff logic (insert/update/delete), the reconciliation of
orphaned blocks, the window boundaries (180 days, no past events), and the
hard invariant: the add-on issues write/delete calls ONLY against its own,
marked blocks — never against a foreign calendar entry.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import settings
from app.busy_sync import (
    BUSY_SYNC_FUTURE_DAYS,
    busy_sync_window,
    run_busy_sync,
    source_key,
)
from app.google_busy import MARKER_KEY, OWNER_KEY, OWNER_VALUE, busy_write_token_path
from app.models import CalendarEvent
from app.sources.google import save_tokens
from app.storage import Storage

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
EVENTS_URL = "https://www.googleapis.com/calendars/primary/events"


def make_storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def write_write_token(path: Path) -> None:
    save_tokens(
        path,
        {
            "client_id": "cid",
            "client_secret": "cs",
            "refresh_token": "rt",
            "access_token": "at-old",
            "access_token_expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
    )


def ensure_source(storage: Storage, source_id: int, name: str = "Roland MV") -> None:
    """Create sources up to source_id so the FK/join in get_events is satisfied.

    Sources auto-increment from 1; create placeholders until the desired id
    exists (tests reference ids 1 and 2 explicitly).
    """
    while len(storage.list_sources()) < source_id:
        storage.add_source(
            type="caldav",
            name=name if len(storage.list_sources()) + 1 == source_id else f"src{source_id}",
            config={"calendar_url": "https://x/cal/"},
        )


def add_mv_event(storage: Storage, source_id: int, uid: str, start: datetime) -> None:
    ensure_source(storage, source_id)
    event = CalendarEvent(
        uid=uid,
        title="MoreValue-Meeting",
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
    )
    storage.sync_events(
        source_id,
        [event],
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
        synced_at=NOW,
    )


class RecordingBackend:
    """Mock Google backend recording every request and modelling the calendar.

    Serves the token endpoint, events.list (filtered by our marker), insert,
    patch and delete. Foreign events (never added by us) are present so tests
    can assert they are never touched.
    """

    def __init__(self, foreign_ids: set[str] | None = None) -> None:
        # id -> event dict (only OUR blocks live here; foreign ids are the
        # ones we assert are never targeted).
        self.own_blocks: dict[str, dict] = {}
        self.foreign_ids = foreign_ids or set()
        self.requests: list[httpx.Request] = []
        self._counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "at-new", "expires_in": 3599})
        path = request.url.path
        # Any request that targets a foreign id is a violation.
        for fid in self.foreign_ids:
            if path.endswith(f"/events/{fid}"):
                raise AssertionError(f"foreign event {fid} was targeted!")
        if request.method == "GET" and path.endswith("/events"):
            return self._list(request)
        if request.method == "POST" and path.endswith("/events"):
            return self._insert(request)
        if request.method == "PATCH":
            return self._patch(request)
        if request.method == "DELETE":
            return self._delete(request)
        return httpx.Response(404)

    def _list(self, request: httpx.Request) -> httpx.Response:
        # Model Google's exact-match privateExtendedProperty filter: only
        # blocks carrying the queried key=value are returned. This mirrors
        # that reconciliation lists by the constant owner marker, never a
        # full scan.
        query = dict(request.url.params).get("privateExtendedProperty", "")
        key, _, value = query.partition("=")
        items = [
            block
            for block in self.own_blocks.values()
            if (block.get("extendedProperties") or {}).get("private", {}).get(key) == value
        ]
        return httpx.Response(200, json={"items": items})

    def _insert(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        assert body["summary"] == "Busy MV"
        assert MARKER_KEY in body["extendedProperties"]["private"]
        self._counter += 1
        event_id = f"gevt-{self._counter}"
        item = {"id": event_id, **body}
        self.own_blocks[event_id] = item
        return httpx.Response(200, json={"id": event_id})

    def _patch(self, request: httpx.Request) -> httpx.Response:
        event_id = request.url.path.rsplit("/", 1)[-1]
        assert event_id in self.own_blocks, "patch must target an own block"
        return httpx.Response(200, json={"id": event_id})

    def _delete(self, request: httpx.Request) -> httpx.Response:
        event_id = request.url.path.rsplit("/", 1)[-1]
        assert event_id in self.own_blocks, "delete must target an own block"
        del self.own_blocks[event_id]
        return httpx.Response(204)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Storage:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    storage = make_storage(tmp_path)
    write_write_token(busy_write_token_path())
    settings.set_busy_sync_enabled(storage, True)
    return storage


async def _run(storage: Storage, backend: RecordingBackend):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(backend.handler)
    ) as client:
        return await run_busy_sync(storage, now=NOW, client=client)


class TestWindow:
    def test_window_is_180_days(self) -> None:
        start, end = busy_sync_window(NOW)
        assert start == datetime(2026, 7, 9, tzinfo=BERLIN)
        assert (end - start).days == BUSY_SYNC_FUTURE_DAYS


@pytest.mark.anyio
class TestDisabledOrNoToken:
    async def test_disabled_does_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        storage = make_storage(tmp_path)
        write_write_token(busy_write_token_path())
        settings.set_busy_sync_enabled(storage, False)
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 0
        assert backend.requests == []

    async def test_no_token_does_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        storage = make_storage(tmp_path)
        settings.set_busy_sync_enabled(storage, True)
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 0
        assert backend.requests == []


@pytest.mark.anyio
class TestInsertUpdateDelete:
    async def test_creates_block_for_new_event(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 1
        assert len(backend.own_blocks) == 1
        assert storage.count_busy_blocks() == 1

    async def test_idempotent_second_run_no_writes(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        await _run(storage, backend)
        result2 = await _run(storage, backend)
        assert result2.inserted == 0
        assert result2.updated == 0
        assert result2.deleted == 0

    async def test_time_change_patches_block(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        await _run(storage, backend)
        # Same uid+start, but end changes -> patch (start is part of the key,
        # so shift only the end via a fresh event with a later end).
        event = CalendarEvent(
            uid="u1",
            title="MoreValue-Meeting",
            start=datetime(2026, 7, 20, 15, tzinfo=UTC),
            end=datetime(2026, 7, 20, 17, tzinfo=UTC),
            all_day=False,
        )
        storage.sync_events(
            1, [event], datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC), synced_at=NOW,
        )
        result = await _run(storage, backend)
        assert result.updated == 1
        assert result.inserted == 0

    async def test_vanished_event_deletes_block(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        await _run(storage, backend)
        # Empty fetch removes the event from storage (within window).
        storage.sync_events(
            1, [], datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC), synced_at=NOW,
        )
        result = await _run(storage, backend)
        assert result.deleted == 1
        assert storage.count_busy_blocks() == 0
        assert backend.own_blocks == {}

    async def test_all_day_event_creates_date_block(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        ensure_source(storage, 1)
        event = CalendarEvent(
            uid="u-day",
            title="Ganztags MV",
            start=date(2026, 7, 20),
            end=date(2026, 7, 21),
            all_day=True,
        )
        storage.sync_events(
            1, [event], datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC), synced_at=NOW,
        )
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 1
        block = next(iter(backend.own_blocks.values()))
        assert block["start"]["date"] == "2026-07-20"

    async def test_only_selected_sources_mirrored(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        add_mv_event(storage, 2, "u2", datetime(2026, 7, 21, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 1

    async def test_deselecting_a_source_deletes_its_blocks(self, env: Storage) -> None:
        # A source was selected and had active blocks; the admin then
        # deselects it. Its desired set becomes empty, so the diff must
        # delete the mapped blocks on the next run (not just stop adding
        # new ones).
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result1 = await _run(storage, backend)
        assert result1.inserted == 1
        assert storage.count_busy_blocks() == 1

        settings.set_busy_sync_source_ids(storage, [])
        result2 = await _run(storage, backend)
        assert result2.deleted == 1
        assert storage.count_busy_blocks() == 0
        assert backend.own_blocks == {}


@pytest.mark.anyio
class TestWindowBoundaries:
    async def test_past_event_not_mirrored(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        # Yesterday relative to NOW (2026-07-09).
        add_mv_event(storage, 1, "u-past", datetime(2026, 7, 8, 15, tzinfo=UTC))
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 0

    async def test_event_beyond_180_days_not_mirrored(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        far = NOW + timedelta(days=BUSY_SYNC_FUTURE_DAYS + 5)
        add_mv_event(storage, 1, "u-far", far)
        backend = RecordingBackend()
        result = await _run(storage, backend)
        assert result.inserted == 0


@pytest.mark.anyio
class TestInvariantAndReconciliation:
    async def test_foreign_events_are_never_touched(self, env: Storage) -> None:
        # The backend raises if any foreign id is ever targeted. The list
        # endpoint only returns our marked blocks, so a foreign event is
        # never even discovered, let alone written/deleted.
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))
        backend = RecordingBackend(foreign_ids={"foreign-boss-meeting"})
        await _run(storage, backend)  # must not raise
        # Delete phase too: remove the event, rerun.
        storage.sync_events(
            1, [], datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC), synced_at=NOW,
        )
        await _run(storage, backend)  # still must not raise

    async def test_orphan_block_without_mapping_is_removed(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        backend = RecordingBackend()
        # Seed an own block in the calendar that has NO mapping row (e.g. a
        # leftover from a previous run whose mapping was lost).
        backend.own_blocks["gevt-orphan"] = {
            "id": "gevt-orphan",
            "summary": "Busy MV",
            "extendedProperties": {
                "private": {MARKER_KEY: "9|gone|x", OWNER_KEY: OWNER_VALUE}
            },
        }
        result = await _run(storage, backend)
        assert result.orphans_removed == 1
        assert "gevt-orphan" not in backend.own_blocks


@pytest.mark.anyio
class TestErrorIsolation:
    async def test_write_error_is_recorded_not_raised(self, env: Storage) -> None:
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))

        def failing(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://oauth2.googleapis.com/token":
                return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
            return httpx.Response(500, json={"error": {"code": 500}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(failing)
        ) as client:
            result = await run_busy_sync(storage, now=NOW, client=client)
        assert result.error is not None
        status = settings.get_busy_sync_status(storage)
        assert status["error"] is not None

    async def test_malformed_google_payload_type_error_is_recorded_not_raised(
        self, env: Storage
    ) -> None:
        # A broken/unexpected Google response body (e.g. "null" instead of an
        # object) makes json.loads(body)["id"] raise TypeError — this must be
        # caught and recorded like any other busy-sync failure, not propagate
        # and take down the periodic calendar sync.
        storage = env
        settings.set_busy_sync_source_ids(storage, [1])
        add_mv_event(storage, 1, "u1", datetime(2026, 7, 20, 15, tzinfo=UTC))

        def broken(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://oauth2.googleapis.com/token":
                return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
            if request.method == "GET":
                return httpx.Response(200, json={"items": []})
            if request.method == "POST":
                return httpx.Response(200, content=b"null")
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
            result = await run_busy_sync(storage, now=NOW, client=client)
        assert result.error is not None
        status = settings.get_busy_sync_status(storage)
        assert status["error"] is not None


class TestSourceKey:
    def test_key_matches_storage_identity(self) -> None:
        event = CalendarEvent(
            uid="u1",
            title="x",
            start=datetime(2026, 7, 20, 15, tzinfo=UTC),
            end=datetime(2026, 7, 20, 16, tzinfo=UTC),
            all_day=False,
        )
        assert source_key(3, event) == "3|u1|2026-07-20T15:00:00+00:00"
