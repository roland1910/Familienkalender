"""Tests for filling the groundwater history (Etappe 46, second part).

Two independent paths write into ``groundwater_readings`` and both are
covered here:

* **path A** (``app.groundwater_history``) scrapes the per-station table
  page of the GKD, which carries roughly the last 62 daily values, and lets
  the daily station list file its current value as that day's reading;
* **path B** (``app.groundwater_import``) imports the full history out of a
  ZIP Roland downloads by hand from the GKD download centre.

Both must be idempotent — the primary key is ``(number, day)`` and a second
run may neither duplicate a row nor change one. Every external call is
mocked; no test here touches the network.
"""

import datetime as dt
import io
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app import groundwater, groundwater_history, groundwater_import
from app.models import GroundwaterStation
from app.settings import get_groundwater_history_status
from app.storage import Storage
from tests.test_groundwater import MockUpstream, gkd_entry, use_upstream

# "Now" for every test: a Sunday inside the freshness window of the sample
# stations below.
NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 9, 20)

PREFIX = "https://www.gkd.bayern.de/de/grundwasser/"


def station_uri(number: str = "16277") -> str:
    """A station address exactly as the GKD overview page hands it over.

    Note the path segment: Johanneskirchen is in Munich but files under
    "kelheim" (the river basin) — which is why the URL is never built from
    the name but always derived from the stored one.
    """
    return f"{PREFIX}oberesstockwerk/kelheim/johanneskirchen-kpa-222-{number}/messwerte"


def make_station(
    number: str = "16277",
    *,
    uri: str | None = None,
    measured: str | None = "19.09.2026 10:00",
    level: float | None = 505.97,
) -> GroundwaterStation:
    return GroundwaterStation(
        number=number,
        name=f"Messstelle {number}",
        lat=48.15,
        lon=11.62,
        tier="upper",
        uri=station_uri(number) if uri is None else uri,
        level_m_nn=level,
        depth_m=4.2,
        measured_at=groundwater.parse_measured_at(measured) if measured else None,
    )


def table_page(rows: list[tuple[str, str]], *, header: bool = True) -> str:
    """The GKD's ``…/messwerte/tabelle`` page: one plain HTML table."""
    cells = ""
    if header:
        cells += "<tr><th>Datum</th><th>Grundwasserstand [m ü. NN]</th></tr>"
    for day, value in rows:
        cells += f"<tr><td>{day}</td><td>{value}</td></tr>"
    return (
        "<html><head><title>Messwerte</title></head><body>"
        f"<table class='tblsort'><tbody>{cells}</tbody></table>"
        "</body></html>"
    )


def descending_days(count: int, *, end: dt.date = TODAY) -> list[tuple[str, str]]:
    """``count`` daily rows, newest first — the real page's sort order."""
    rows = []
    for offset in range(count):
        day = (end - dt.timedelta(days=offset)).strftime("%d.%m.%Y")
        rows.append((day, f"{505.0 + offset * 0.01:.2f}".replace(".", ",")))
    return rows


class TableUpstream:
    """Mocked GKD table pages, keyed by URL, with a request counter."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        *,
        failing: set[str] | None = None,
        down: bool = False,
    ) -> None:
        self.pages = pages or {}
        self.failing = failing or set()
        self.down = down
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused")
        url = str(request.url)
        if url in self.failing:
            return httpx.Response(500, text="Fehler")
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, text="Nicht gefunden")
        return httpx.Response(200, text=page)

    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        def create() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler),
                headers={"User-Agent": groundwater.USER_AGENT},
            )

        return create


def use_tables(monkeypatch: pytest.MonkeyPatch, mock: TableUpstream) -> None:
    monkeypatch.setattr(groundwater_history, "create_table_client", mock.client_factory())
    # The politeness pause is real in production (see REQUEST_PAUSE_SECONDS)
    # but would make every test sleep a second per station.
    monkeypatch.setattr(groundwater_history, "REQUEST_PAUSE_SECONDS", 0.0)


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


# -- path A: parsing the table page -----------------------------------------


class TestParseTablePage:
    def test_reads_the_day_and_the_level(self) -> None:
        page = table_page([("17.09.2026", "505,97")])
        assert groundwater_history.parse_table_page(page) == [("2026-09-17", 505.97)]

    def test_the_descending_page_comes_back_oldest_first(self) -> None:
        page = table_page(
            [("17.09.2026", "505,97"), ("16.09.2026", "505,95"), ("15.09.2026", "506,28")]
        )
        days = [day for day, _ in groundwater_history.parse_table_page(page)]
        assert days == ["2026-09-15", "2026-09-16", "2026-09-17"]

    def test_the_header_row_is_ignored(self) -> None:
        page = table_page([("17.09.2026", "505,97")], header=True)
        assert len(groundwater_history.parse_table_page(page)) == 1

    def test_the_full_page_yields_every_value(self) -> None:
        page = table_page(descending_days(62))
        assert len(groundwater_history.parse_table_page(page)) == 62

    def test_broken_cells_are_skipped_one_by_one(self) -> None:
        page = table_page(
            [
                ("17.09.2026", "505,97"),
                ("", "505,00"),
                ("16.09.2026", "--"),
                ("32.13.2026", "505,00"),
                ("15.09.2026", "keine Zahl"),
                ("14.09.2026", "506,28"),
            ]
        )
        assert groundwater_history.parse_table_page(page) == [
            ("2026-09-14", 506.28),
            ("2026-09-17", 505.97),
        ]

    def test_a_page_without_a_table_yields_nothing(self) -> None:
        assert groundwater_history.parse_table_page("<html><body>Wartung</body></html>") == []

    def test_an_empty_page_yields_nothing(self) -> None:
        assert groundwater_history.parse_table_page("") == []

    def test_unclosed_markup_does_not_raise(self) -> None:
        broken = "<html><body><table><tr><td>17.09.2026<td>505,97</table>"
        assert groundwater_history.parse_table_page(broken) == [("2026-09-17", 505.97)]

    def test_markup_inside_a_cell_does_not_confuse_the_parser(self) -> None:
        page = "<table><tr><td><span>17.09.2026</span></td><td><b>505,97</b></td></tr></table>"
        assert groundwater_history.parse_table_page(page) == [("2026-09-17", 505.97)]

    def test_a_duplicate_day_yields_a_single_row(self) -> None:
        page = table_page([("17.09.2026", "505,97"), ("17.09.2026", "505,97")])
        assert groundwater_history.parse_table_page(page) == [("2026-09-17", 505.97)]


# -- path A: deriving the table URL from the stored address ------------------


class TestTableUrl:
    def test_the_table_hangs_off_the_stored_address(self) -> None:
        assert groundwater_history.table_url(station_uri()) == station_uri() + "/tabelle"

    def test_a_trailing_slash_is_tolerated(self) -> None:
        assert groundwater_history.table_url(station_uri() + "/") == station_uri() + "/tabelle"

    def test_an_address_that_already_points_at_the_table_is_kept(self) -> None:
        already = station_uri() + "/tabelle"
        assert groundwater_history.table_url(already) == already

    def test_an_address_outside_the_groundwater_section_is_refused(self) -> None:
        assert groundwater_history.table_url("https://www.gkd.bayern.de/de/downloadcenter") is None

    def test_a_foreign_host_is_refused(self) -> None:
        assert groundwater_history.table_url("https://evil.example.com/de/grundwasser/x") is None

    def test_a_host_that_only_starts_like_the_gkd_is_refused(self) -> None:
        assert (
            groundwater_history.table_url(
                "https://www.gkd.bayern.de.evil.example.com/de/grundwasser/x"
            )
            is None
        )

    def test_a_literal_traversal_is_refused(self) -> None:
        assert groundwater_history.table_url(f"{PREFIX}oberes/../../../etc/passwd") is None

    def test_a_percent_encoded_traversal_is_refused(self) -> None:
        # httpx.URL.join normalizes a literal ".." away but leaves "%2e%2e"
        # standing — the very trap from Etappe 41b.
        assert groundwater_history.table_url(f"{PREFIX}oberes/%2e%2e/%2e%2e/intern") is None

    def test_an_address_with_a_query_is_refused(self) -> None:
        assert groundwater_history.table_url(f"{PREFIX}oberes/x/messwerte?zr=jahr") is None

    def test_an_empty_address_is_refused(self) -> None:
        assert groundwater_history.table_url("") is None
        assert groundwater_history.table_url(PREFIX) is None

    def test_plain_http_is_refused(self) -> None:
        assert groundwater_history.table_url(PREFIX.replace("https", "http") + "x") is None


# -- path A: which stations get fetched at all -------------------------------


def coverage(days: list[str]) -> dict:
    """A coverage entry as the storage layer reports it."""
    return groundwater_history.ReadingCoverage(len(days), min(days), max(days))


class TestStationSelection:
    def test_a_station_without_any_reading_is_due(self) -> None:
        assert groundwater_history.stations_needing_history([make_station()], {}, NOW)

    def test_a_filled_station_is_not_fetched_again(self) -> None:
        filled = {"16277": coverage(["2026-07-18", "2026-09-19"])}
        assert groundwater_history.stations_needing_history([make_station()], filled, NOW) == []

    def test_a_station_with_a_long_gap_is_due(self) -> None:
        # Two months of history but nothing for the last three weeks: the
        # add-on was off, and the table page can close exactly that gap.
        stale = {"16277": coverage(["2026-06-01", "2026-08-25"])}
        assert groundwater_history.stations_needing_history([make_station()], stale, NOW)

    def test_a_single_fresh_value_is_not_enough(self) -> None:
        # The daily list writes today's value BEFORE the backfill runs — that
        # one row must not look like "history already there".
        single = {"16277": coverage(["2026-09-20"])}
        assert groundwater_history.stations_needing_history([make_station()], single, NOW)

    def test_an_abandoned_station_is_never_fetched(self) -> None:
        # About half the stations in the radius have been silent for years;
        # scraping their table page every hour would be pure noise.
        old = make_station(measured="19.09.2019 10:00")
        assert groundwater_history.stations_needing_history([old], {}, NOW) == []

    def test_a_station_without_a_usable_address_is_left_out(self) -> None:
        nowhere = make_station(uri="")
        assert groundwater_history.stations_needing_history([nowhere], {}, NOW) == []


# -- path A: the backfill run ------------------------------------------------


@pytest.mark.anyio
class TestBackfillHistory:
    async def test_every_due_station_is_filled(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        stations = [make_station("16277"), make_station("16704")]
        storage.replace_groundwater_stations(stations)
        upstream = TableUpstream(
            {
                station_uri("16277") + "/tabelle": table_page(descending_days(62)),
                station_uri("16704") + "/tabelle": table_page(descending_days(62)),
            }
        )
        use_tables(monkeypatch, upstream)

        filled = await groundwater_history.backfill_history(storage, now=NOW)

        assert filled == 2
        assert len(storage.list_groundwater_readings("16277")) == 62
        assert len(storage.list_groundwater_readings("16704")) == 62
        assert len(upstream.requests) == 2

    async def test_the_user_agent_identifies_the_add_on(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        upstream = TableUpstream({station_uri() + "/tabelle": table_page(descending_days(62))})
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)

        assert upstream.requests[0].headers["User-Agent"] == groundwater.USER_AGENT

    async def test_running_twice_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        upstream = TableUpstream({station_uri() + "/tabelle": table_page(descending_days(62))})
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)
        first = storage.list_groundwater_readings("16277")
        await groundwater_history.backfill_history(storage, now=NOW, force=True)

        assert storage.list_groundwater_readings("16277") == first

    async def test_a_filled_station_causes_no_request(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        upstream = TableUpstream({station_uri() + "/tabelle": table_page(descending_days(62))})
        use_tables(monkeypatch, upstream)
        await groundwater_history.backfill_history(storage, now=NOW)
        before = len(upstream.requests)

        await groundwater_history.backfill_history(storage, now=NOW, force=True)

        assert len(upstream.requests) == before

    async def test_an_unsafe_address_triggers_no_request(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        hostile = make_station(uri="https://evil.example.com/de/grundwasser/x/messwerte")
        storage.replace_groundwater_stations([hostile])
        upstream = TableUpstream()
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)

        assert upstream.requests == []
        assert storage.list_groundwater_readings("16277") == []

    async def test_a_broken_station_does_not_end_the_run(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station("16277"), make_station("16704")])
        upstream = TableUpstream(
            {station_uri("16704") + "/tabelle": table_page(descending_days(62))},
            failing={station_uri("16277") + "/tabelle"},
        )
        use_tables(monkeypatch, upstream)

        filled = await groundwater_history.backfill_history(storage, now=NOW)

        assert filled == 1
        assert len(storage.list_groundwater_readings("16704")) == 62

    async def test_it_stops_after_too_many_consecutive_errors(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        count = groundwater_history.MAX_CONSECUTIVE_ERRORS + 3
        storage.replace_groundwater_stations(
            [make_station(str(20000 + index)) for index in range(count)]
        )
        upstream = TableUpstream(down=True)
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)

        assert len(upstream.requests) == groundwater_history.MAX_CONSECUTIVE_ERRORS

    async def test_it_pauses_between_two_stations(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station("16277"), make_station("16704")])
        upstream = TableUpstream(
            {
                station_uri("16277") + "/tabelle": table_page(descending_days(62)),
                station_uri("16704") + "/tabelle": table_page(descending_days(62)),
            }
        )
        use_tables(monkeypatch, upstream)
        pauses: list[int] = []

        async def fake_pause() -> None:
            pauses.append(1)

        monkeypatch.setattr(groundwater_history, "_pause", fake_pause)
        await groundwater_history.backfill_history(storage, now=NOW)

        assert len(pauses) == 1  # between the two, not before the first

    async def test_no_more_than_the_per_run_cap_is_fetched(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        monkeypatch.setattr(groundwater_history, "MAX_STATIONS_PER_RUN", 2)
        storage.replace_groundwater_stations(
            [make_station(str(20000 + index)) for index in range(5)]
        )
        upstream = TableUpstream(
            {
                station_uri(str(20000 + index)) + "/tabelle": table_page(descending_days(62))
                for index in range(5)
            }
        )
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)

        assert len(upstream.requests) == 2

    async def test_a_second_run_within_the_hour_does_nothing(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station("16277"), make_station("16704")])
        upstream = TableUpstream(
            {station_uri("16277") + "/tabelle": table_page(descending_days(62))}
        )
        use_tables(monkeypatch, upstream)
        await groundwater_history.backfill_history(storage, now=NOW)
        before = len(upstream.requests)

        await groundwater_history.backfill_history(storage, now=NOW + dt.timedelta(minutes=5))

        assert len(upstream.requests) == before

    async def test_the_next_hour_continues(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        monkeypatch.setattr(groundwater_history, "MAX_STATIONS_PER_RUN", 1)
        storage.replace_groundwater_stations([make_station("16277"), make_station("16704")])
        upstream = TableUpstream(
            {
                station_uri("16277") + "/tabelle": table_page(descending_days(62)),
                station_uri("16704") + "/tabelle": table_page(descending_days(62)),
            }
        )
        use_tables(monkeypatch, upstream)
        await groundwater_history.backfill_history(storage, now=NOW)

        await groundwater_history.backfill_history(storage, now=NOW + dt.timedelta(hours=2))

        assert len(upstream.requests) == 2

    async def test_the_status_records_the_run(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        upstream = TableUpstream({station_uri() + "/tabelle": table_page(descending_days(62))})
        use_tables(monkeypatch, upstream)

        await groundwater_history.backfill_history(storage, now=NOW)

        status = get_groundwater_history_status(storage)
        assert status["last_run"] == NOW.isoformat()
        assert status["stations"] == 1
        assert status["readings"] == 62
        assert status["error"] is None

    async def test_a_failed_run_records_the_error_without_raising(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        use_tables(monkeypatch, TableUpstream(down=True))

        await groundwater_history.backfill_history(storage, now=NOW)

        assert "nicht erreichbar" in get_groundwater_history_status(storage)["error"]
        assert storage.list_groundwater_readings("16277") == []

    async def test_an_empty_table_page_stores_nothing(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.replace_groundwater_stations([make_station()])
        use_tables(monkeypatch, TableUpstream({station_uri() + "/tabelle": table_page([])}))

        await groundwater_history.backfill_history(storage, now=NOW)

        assert storage.list_groundwater_readings("16277") == []


# -- path A: the daily station list feeds the history ------------------------


@pytest.mark.anyio
class TestDailyReadingFromTheStationList:
    async def test_the_daily_refresh_files_the_current_value(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(
            monkeypatch,
            MockUpstream(upper=[gkd_entry(level="508,98", measured="19.09.2026 10:00")]),
        )

        await groundwater.refresh_stations(storage, now=NOW)

        assert storage.list_groundwater_readings("16704") == [("2026-09-19", 508.98)]

    async def test_a_second_refresh_on_the_same_day_does_not_duplicate(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(level="508,98")]))
        await groundwater.refresh_stations(storage, now=NOW)

        await groundwater.refresh_stations(storage, now=NOW, force=True)

        assert len(storage.list_groundwater_readings("16704")) == 1

    async def test_a_corrected_value_replaces_the_days_reading(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(level="508,98")]))
        await groundwater.refresh_stations(storage, now=NOW)
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(level="509,10")]))

        await groundwater.refresh_stations(storage, now=NOW, force=True)

        assert storage.list_groundwater_readings("16704") == [("2026-09-19", 509.10)]

    async def test_the_next_day_adds_a_second_reading(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(measured="19.09.2026 10:00")]))
        await groundwater.refresh_stations(storage, now=NOW)
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(measured="20.09.2026 10:00")]))

        await groundwater.refresh_stations(storage, now=NOW, force=True)

        assert len(storage.list_groundwater_readings("16704")) == 2

    async def test_a_station_without_a_value_stores_nothing(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(level=None)]))

        await groundwater.refresh_stations(storage, now=NOW)

        assert storage.list_groundwater_readings("16704") == []

    async def test_a_failed_refresh_keeps_the_stored_readings(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry(level="508,98")]))
        await groundwater.refresh_stations(storage, now=NOW)
        use_upstream(monkeypatch, MockUpstream(down=True))

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(days=2))

        assert storage.list_groundwater_readings("16704") == [("2026-09-19", 508.98)]

    def test_the_local_day_decides_not_the_utc_one(self) -> None:
        # 00:30 Europe/Berlin on the 20th is 22:30 UTC on the 19th — the
        # reading belongs to the local day the GKD published it for.
        station = make_station(measured="20.09.2026 00:30", level=505.5)
        assert groundwater.daily_readings([station]) == {"16277": ("2026-09-20", 505.5)}


# -- path B: importing the manually downloaded ZIP ---------------------------

CSV_HEADER = (
    'Quelle:;"Bayerisches Landesamt für Umwelt, www.gkd.bayern.de"\r\n'
    'Datenbankabfrage:;"20.09.2026 00:00"\r\n'
    "Zeitbezug:;MEZ\r\n"
    'Messstellen-Name:;"JOHANNESKIRCHEN KPA 222"\r\n'
    "Messstellen-Nr.:;{number}\r\n"
    'Ostwert:;696784;Nordwert:;5338502;"ETRS89 / UTM Zone 32N"\r\n'
    "\r\n"
    'Datum;"Grundwasserstand [m ü. NN]";Prüfstatus\r\n'
)


def csv_text(number: str, rows: list[tuple[str, str]], *, status: str = "Rohdaten") -> str:
    """One GKD export file: UTF-8 (BOM added when zipped), semicolons, comma."""
    body = "".join(f"{day};{value};{status}\r\n" for day, value in rows)
    return CSV_HEADER.format(number=number) + body


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            # The real export is UTF-8 WITH a BOM.
            archive.writestr(name, "﻿" + text)
    return path


@pytest.fixture
def import_storage(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "import.db")
    store.replace_groundwater_stations([make_station("16277"), make_station("16704")])
    return store


class TestZipImport:
    def test_the_readings_are_stored(self, tmp_path: Path, import_storage: Storage) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "16277_1.csv": csv_text(
                    "16277", [("2026-09-01", "506,04"), ("2026-09-02", "506,08")]
                )
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.readings == 2
        assert result.stations == 1
        assert import_storage.list_groundwater_readings("16277") == [
            ("2026-09-01", 506.04),
            ("2026-09-02", 506.08),
        ]

    def test_the_number_comes_from_the_header_not_the_filename(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {"irgendwas-16704.csv": csv_text("16277", [("2026-09-01", "506,04")])},
        )

        groundwater_import.import_zip_archives(import_storage, [archive])

        assert import_storage.list_groundwater_readings("16277") == [("2026-09-01", 506.04)]
        assert import_storage.list_groundwater_readings("16704") == []

    def test_several_files_of_one_station_are_merged(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "teil1.csv": csv_text("16277", [("1979-05-02", "505,10")]),
                "teil2.csv": csv_text("16277", [("2026-09-01", "506,04")]),
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.stations == 1
        assert [day for day, _ in import_storage.list_groundwater_readings("16277")] == [
            "1979-05-02",
            "2026-09-01",
        ]

    def test_several_stations_land_in_their_own_rows(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "a.csv": csv_text("16277", [("2026-09-01", "506,04")]),
                "b.csv": csv_text("16704", [("2026-09-01", "508,98")]),
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.stations == 2
        assert import_storage.list_groundwater_readings("16704") == [("2026-09-01", 508.98)]

    def test_a_checked_status_is_imported_just_like_raw_data(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {"a.csv": csv_text("16277", [("1999-01-01", "505,10")], status="Geprueft")},
        )

        groundwater_import.import_zip_archives(import_storage, [archive])

        assert import_storage.list_groundwater_readings("16277") == [("1999-01-01", 505.10)]

    def test_an_unknown_station_is_skipped_and_counted(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip", {"a.csv": csv_text("99999", [("2026-09-01", "506,04")])}
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.unknown_stations == ["99999"]
        assert result.readings == 0

    def test_broken_rows_are_skipped_one_by_one(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "a.csv": csv_text(
                    "16277",
                    [
                        ("2026-09-01", "506,04"),
                        ("2026-13-45", "506,00"),
                        ("2026-09-03", "keine Zahl"),
                        ("2026-09-04", ""),
                        ("2026-09-05", "506,10"),
                    ],
                )
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.readings == 2
        assert result.skipped_rows == 3

    def test_a_file_without_a_station_number_is_skipped(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(tmp_path / "gkd.zip", {"a.csv": "Datum;Wert\r\n2026-09-01;506,04\r\n"})

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.readings == 0
        assert result.skipped_files == 1

    def test_a_non_csv_entry_is_ignored(self, tmp_path: Path, import_storage: Storage) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "logo.png": "nicht wirklich ein Bild",
                "a.csv": csv_text("16277", [("2026-09-01", "506,04")]),
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.files == 1
        assert result.readings == 1

    def test_zip_slip_entries_are_never_read(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                "../../etc/passwd.csv": csv_text("16277", [("2026-09-01", "506,04")]),
                "/absolut.csv": csv_text("16277", [("2026-09-02", "506,08")]),
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.readings == 0
        assert result.skipped_files == 2
        assert import_storage.list_groundwater_readings("16277") == []
        assert not (tmp_path.parent / "etc").exists()

    def test_too_many_entries_are_refused(
        self, tmp_path: Path, import_storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(groundwater_import, "MAX_ENTRIES", 2)
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                f"teil{index}.csv": csv_text("16277", [(f"2026-09-0{index + 1}", "506,04")])
                for index in range(4)
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.files == 2

    def test_an_oversized_member_is_skipped(
        self, tmp_path: Path, import_storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(groundwater_import, "MAX_MEMBER_BYTES", 64)
        archive = make_zip(
            tmp_path / "gkd.zip",
            {"a.csv": csv_text("16277", [("2026-09-01", "506,04")])},
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.readings == 0
        assert result.skipped_files == 1

    def test_the_total_size_is_capped(
        self, tmp_path: Path, import_storage: Storage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(groundwater_import, "MAX_TOTAL_BYTES", 100)
        archive = make_zip(
            tmp_path / "gkd.zip",
            {
                f"teil{index}.csv": csv_text("16277", [(f"2026-09-0{index + 1}", "506,04")])
                for index in range(4)
            },
        )

        result = groundwater_import.import_zip_archives(import_storage, [archive])

        assert result.files < 4

    def test_importing_twice_changes_nothing(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        archive = make_zip(
            tmp_path / "gkd.zip",
            {"a.csv": csv_text("16277", [("2026-09-01", "506,04"), ("2026-09-02", "506,08")])},
        )
        groundwater_import.import_zip_archives(import_storage, [archive])
        first = import_storage.list_groundwater_readings("16277")

        groundwater_import.import_zip_archives(import_storage, [archive])

        assert import_storage.list_groundwater_readings("16277") == first

    def test_the_backfill_and_the_import_share_the_table(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        import_storage.upsert_groundwater_readings("16277", [("2026-09-01", 1.0)])
        archive = make_zip(
            tmp_path / "gkd.zip",
            {"a.csv": csv_text("16277", [("2026-09-01", "506,04"), ("1979-05-02", "505,10")])},
        )

        groundwater_import.import_zip_archives(import_storage, [archive])

        assert import_storage.list_groundwater_readings("16277") == [
            ("1979-05-02", 505.10),
            ("2026-09-01", 506.04),
        ]

    def test_a_broken_archive_is_reported_not_raised(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        broken = tmp_path / "kaputt.zip"
        broken.write_bytes(b"das ist kein ZIP")

        result = groundwater_import.import_zip_archives(import_storage, [broken])

        assert result.errors
        assert result.readings == 0

    def test_a_missing_archive_is_reported_not_raised(
        self, tmp_path: Path, import_storage: Storage
    ) -> None:
        result = groundwater_import.import_zip_archives(
            import_storage, [tmp_path / "gibtesnicht.zip"]
        )

        assert result.errors

    def test_several_archives_are_merged(self, tmp_path: Path, import_storage: Storage) -> None:
        first = make_zip(
            tmp_path / "a.zip", {"a.csv": csv_text("16277", [("1979-05-02", "505,10")])}
        )
        second = make_zip(
            tmp_path / "b.zip", {"b.csv": csv_text("16277", [("2026-09-01", "506,04")])}
        )

        result = groundwater_import.import_zip_archives(import_storage, [first, second])

        assert result.archives == 2
        assert len(import_storage.list_groundwater_readings("16277")) == 2


class TestSummaryText:
    def test_a_successful_import_is_summarized_in_german(self) -> None:
        result = groundwater_import.ImportResult(
            archives=1, files=3, stations=2, readings=17368
        )
        text = groundwater_import.summary_text(result)
        assert "1 Archiv" in text
        assert "3 Dateien" in text
        assert "2 Messstellen" in text
        assert "17368 Werte" in text

    def test_skipped_items_are_named(self) -> None:
        result = groundwater_import.ImportResult(
            archives=1, skipped_files=2, skipped_rows=5, unknown_stations=["99999"]
        )
        text = groundwater_import.summary_text(result)
        assert "2 Dateien übersprungen" in text
        assert "5 Zeilen übersprungen" in text
        assert "99999" in text


class TestParseCsv:
    def test_the_bom_does_not_hide_the_first_header(self) -> None:
        number, readings, _ = groundwater_import.parse_csv(
            "﻿" + csv_text("16277", [("2026-09-01", "506,04")])
        )
        assert number == "16277"
        assert readings == {"2026-09-01": 506.04}

    def test_a_quoted_number_is_read(self) -> None:
        text = csv_text("16277", []).replace("Messstellen-Nr.:;16277", 'Messstellen-Nr.:;"16277"')
        number, _, _ = groundwater_import.parse_csv(text)
        assert number == "16277"

    def test_a_non_numeric_number_is_refused(self) -> None:
        text = csv_text("16277", []).replace(
            "Messstellen-Nr.:;16277", "Messstellen-Nr.:;../../etc"
        )
        number, _, _ = groundwater_import.parse_csv(text)
        assert number is None

    def test_an_implausible_year_is_skipped(self) -> None:
        _, readings, skipped = groundwater_import.parse_csv(
            csv_text("16277", [("1492-09-01", "506,04")])
        )
        assert readings == {}
        assert skipped == 1

    def test_a_file_that_is_not_a_csv_at_all_yields_nothing(self) -> None:
        number, readings, _ = groundwater_import.parse_csv("irgendein Text ohne Struktur")
        assert number is None
        assert readings == {}


def test_a_stream_without_a_bom_is_read_too(tmp_path: Path) -> None:
    # Defensive: the export carries a BOM today, a relaunch might drop it.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", csv_text("16277", [("2026-09-01", "506,04")]))
    path = tmp_path / "nobom.zip"
    path.write_bytes(buffer.getvalue())
    store = Storage(tmp_path / "s.db")
    store.replace_groundwater_stations([make_station("16277")])

    result = groundwater_import.import_zip_archives(store, [path])

    assert result.readings == 1
