"""Tests for the groundwater view (/api/groundwater) with a mocked GKD/NID.

GKD Bayern publishes no API — the station list is scraped out of an inline
JSON array on the overview pages (see app/groundwater.py). That makes the
parser the risky part: it has to survive a relaunch of those pages without
taking the whole view down, and it must never let a failed or empty fetch
blank the stored stations. Every external call here is mocked; no test
touches the network.
"""

import datetime as dt
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import groundwater
from app.main import app
from app.models import GroundwaterStation
from app.storage import Storage

# A time inside the freshness window of the sample readings below.
NOW = dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC)


def gkd_entry(
    number: str = "16704",
    *,
    name: str = "München KP 95",
    lat: str = "48.1288",
    lon: str = "11.5863",
    level: str | None = "508,98",
    depth: str | None = "4,28",
    measured: str | None = "19.09.2026 10:00",
    aquifer: str = "Quartär",
    uri: str | None = None,
) -> dict:
    """One station object exactly as the GKD overview page embeds it."""
    entry: dict = {
        "p": number,
        "n": name,
        "uri": uri
        if uri is not None
        else f"https://www.gkd.bayern.de/de/grundwasser/oberesstockwerk/kelheim/x-{number}/messwerte",
        "k": "circle0",
        "l": ["ap"],
        "lon": lon,
        "lat": lat,
        "gknr": 0,
        "gwl": aquifer,
    }
    if measured is not None:
        entry["d"] = measured
    if level is not None:
        entry["w"] = level
    if depth is not None:
        entry["w2"] = depth
    return entry


def nid_entry(
    number: str = "16704", *, situation: str = "niedrig", klasse: int = 1
) -> dict:
    """One NID low-water object (only the fields we join on)."""
    return {
        "thema": "grundwasser",
        "gkr": 4468000.0,
        "gkh": 5333000.0,
        "klasse": klasse,
        "name": "MÜNCHEN KP 95",
        "situation": situation,
        "datum": "17.09.2026",
        "pegelnummer": number,
        "wertDisplay": "508,98",
        "flurabstand": "4,28",
    }


def pointer_page(entries: list[dict], *, escape_slashes: bool = False) -> str:
    """The inline-JSON page shape both GKD and NID serve."""
    payload = json.dumps(entries, ensure_ascii=False)
    if escape_slashes:
        payload = payload.replace("/", r"\/")
    return (
        "<html><head><title>Messstellen</title></head><body>"
        '<script src="/assets/js/leaflet_maps.js"></script><script>'
        'LfUMap.init({"thema":"grundwasser","gknr":0,"pointer":' + payload + ',"zoom":7});'
        "</script></body></html>"
    )


class MockUpstream:
    """Mocked GKD + NID pages with a per-URL request counter."""

    def __init__(
        self,
        *,
        upper: list[dict] | None = None,
        deep: list[dict] | None = None,
        nid: list[dict] | None = None,
        down: bool = False,
        status: int = 200,
        body: str | None = None,
        stream: bool = False,
    ) -> None:
        self.upper = upper if upper is not None else []
        self.deep = deep if deep is not None else []
        self.nid = nid if nid is not None else []
        self.down = down
        self.status = status
        self.body = body
        self.stream = stream
        self.requests: list[httpx.Request] = []

    def _page(self, path: str) -> str:
        if self.body is not None:
            return self.body
        if path.endswith("oberesstockwerk"):
            return pointer_page(self.upper, escape_slashes=True)
        if path.endswith("tieferestockwerke"):
            return pointer_page(self.deep, escape_slashes=True)
        return pointer_page(self.nid)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused")
        if self.status != 200:
            return httpx.Response(self.status, text="Fehler")
        text = self._page(request.url.path)
        if self.stream:
            # The real GKD page arrives chunked, WITHOUT a Content-Length —
            # the size limit must not depend on that header.
            async def chunks() -> AsyncIterator[bytes]:
                yield text.encode("utf-8")

            return httpx.Response(200, content=chunks())
        return httpx.Response(200, text=text)

    def client_factory(self, base_url: str = "") -> Callable[[], httpx.AsyncClient]:
        def create() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler),
                base_url=base_url,
                headers={"User-Agent": groundwater.USER_AGENT},
            )

        return create

    @property
    def gkd_requests(self) -> list[httpx.Request]:
        return [item for item in self.requests if "gkd.bayern.de" in str(item.url)]

    @property
    def nid_requests(self) -> list[httpx.Request]:
        return [item for item in self.requests if "nid.bayern.de" in str(item.url)]


def use_upstream(monkeypatch: pytest.MonkeyPatch, mock: MockUpstream) -> None:
    monkeypatch.setattr(
        groundwater, "create_gkd_client", mock.client_factory(groundwater.GKD_HOST)
    )
    monkeypatch.setattr(groundwater, "create_nid_client", mock.client_factory())


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return TestClient(app, client=("127.0.0.1", 50000))


def api_storage(tmp_path: Path) -> Storage:
    """The very Storage the API layer uses under a DATA_DIR of tmp_path."""
    return Storage(tmp_path / "familienkalender.db")


class TestParseStationList:
    def test_reads_every_field_of_a_station(self) -> None:
        [station] = groundwater.parse_station_list(
            pointer_page([gkd_entry()]), "upper"
        )

        assert station.number == "16704"
        assert station.name == "München KP 95"
        assert station.lat == pytest.approx(48.1288)
        assert station.lon == pytest.approx(11.5863)
        assert station.tier == "upper"
        assert station.aquifer == "Quartär"
        # Decimal comma, not a decimal point.
        assert station.level_m_nn == pytest.approx(508.98)
        assert station.depth_m == pytest.approx(4.28)
        assert station.uri.startswith("https://www.gkd.bayern.de/")

    def test_ground_elevation_is_level_plus_depth(self) -> None:
        # Johanneskirchen KPA 222 sits at 514.58 m: 505.95 + 8.63.
        [station] = groundwater.parse_station_list(
            pointer_page(
                [gkd_entry("16277", level="505,95", depth="8,63", lat="48.1693", lon="11.6468")]
            ),
            "upper",
        )

        assert station.level_m_nn + station.depth_m == pytest.approx(514.58)

    def test_local_timestamp_becomes_utc(self) -> None:
        # "19.09.2026 15:00" is Europe/Berlin summer time = 13:00 UTC.
        [station] = groundwater.parse_station_list(
            pointer_page([gkd_entry(measured="19.09.2026 15:00")]), "upper"
        )

        assert station.measured_at == "2026-09-19T13:00:00+00:00"

    def test_escaped_slashes_in_the_uri_are_resolved(self) -> None:
        [station] = groundwater.parse_station_list(
            pointer_page([gkd_entry()], escape_slashes=True), "upper"
        )

        assert "\\/" not in station.uri
        assert station.uri.startswith("https://www.gkd.bayern.de/de/grundwasser/")

    def test_dashes_and_missing_values_become_none(self) -> None:
        page = pointer_page(
            [gkd_entry(level="--", depth="--", measured=None), gkd_entry("2", level=None)]
        )

        first, second = groundwater.parse_station_list(page, "upper")

        assert (first.level_m_nn, first.depth_m, first.measured_at) == (None, None, None)
        assert second.level_m_nn is None

    def test_non_numeric_values_become_none_instead_of_raising(self) -> None:
        [station] = groundwater.parse_station_list(
            pointer_page([gkd_entry(level="kein Wert", depth="")]), "upper"
        )

        assert station.level_m_nn is None
        assert station.depth_m is None

    def test_an_unreadable_timestamp_becomes_none(self) -> None:
        [station] = groundwater.parse_station_list(
            pointer_page([gkd_entry(measured="irgendwann")]), "upper"
        )

        assert station.measured_at is None

    def test_broken_entries_are_skipped_one_by_one(self) -> None:
        page = pointer_page(
            [
                gkd_entry("1"),
                "kein Objekt",
                {"n": "ohne Nummer", "lat": "48.1", "lon": "11.5"},
                {"p": "3", "n": "ohne Koordinaten"},
                {"p": "4", "n": "krumme Koordinaten", "lat": "Norden", "lon": "11.5"},
                gkd_entry("5"),
            ]
        )

        stations = groundwater.parse_station_list(page, "upper")

        assert [s.number for s in stations] == ["1", "5"]

    def test_a_page_without_the_json_yields_nothing(self) -> None:
        # A relaunch of the GKD site must not take the whole view down.
        assert groundwater.parse_station_list("<html>Wartungsarbeiten</html>", "upper") == []

    def test_unparseable_json_yields_nothing(self) -> None:
        assert groundwater.parse_station_list('<script>init({"pointer":[{,]})', "deep") == []

    def test_the_tier_is_carried_into_every_station(self) -> None:
        [station] = groundwater.parse_station_list(pointer_page([gkd_entry()]), "deep")

        assert station.tier == "deep"


class TestParseNidSituations:
    def test_maps_station_number_to_situation_and_class(self) -> None:
        result = groundwater.parse_nid_situations(
            pointer_page([nid_entry("16704", situation="sehr niedrig", klasse=2)])
        )

        assert result == {"16704": ("sehr niedrig", 2)}

    def test_unclassified_stations_are_left_out(self) -> None:
        # The NID uses klasse -2 / "Keine Klassifizierung" for stations it
        # does not rate; that is not a classification, so it is dropped.
        result = groundwater.parse_nid_situations(
            pointer_page(
                [
                    nid_entry("1", situation="Keine Klassifizierung", klasse=-2),
                    nid_entry("2", situation="kein Niedrigwasser", klasse=0),
                ]
            )
        )

        assert result == {"2": ("kein Niedrigwasser", 0)}

    def test_broken_entries_are_skipped(self) -> None:
        result = groundwater.parse_nid_situations(
            pointer_page(
                [
                    "kein Objekt",
                    {"situation": "niedrig", "klasse": 1},
                    {"pegelnummer": "3", "klasse": "eins", "situation": "niedrig"},
                    nid_entry("4"),
                ]
            )
        )

        assert result == {"4": ("niedrig", 1)}

    def test_a_changed_page_yields_an_empty_mapping(self) -> None:
        assert groundwater.parse_nid_situations("<html>Umbau</html>") == {}


class TestDistance:
    def test_the_distance_to_munich_itself_is_zero(self) -> None:
        assert groundwater.haversine_km(
            groundwater.MUNICH_LAT,
            groundwater.MUNICH_LON,
            groundwater.MUNICH_LAT,
            groundwater.MUNICH_LON,
        ) == pytest.approx(0.0, abs=1e-9)

    def test_a_known_distance(self) -> None:
        # Munich → Augsburg (48.3705/10.8978) is about 60 km.
        distance = groundwater.haversine_km(
            groundwater.MUNICH_LAT, groundwater.MUNICH_LON, 48.3705, 10.8978
        )

        assert distance == pytest.approx(58.0, abs=3.0)

    def test_the_radius_includes_and_excludes_at_the_boundary(self) -> None:
        # One degree of latitude is ~111.2 km, so ±0.4° is ~44 km (inside)
        # and ±0.5° is ~55.6 km (outside).
        assert groundwater.is_near_munich(groundwater.MUNICH_LAT + 0.4, groundwater.MUNICH_LON)
        assert not groundwater.is_near_munich(
            groundwater.MUNICH_LAT + 0.5, groundwater.MUNICH_LON
        )


class TestFetchStations:
    @pytest.mark.anyio
    async def test_both_tiers_are_fetched_and_joined_with_the_nid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(
            upper=[gkd_entry("16704")],
            deep=[gkd_entry("16806", lat="48.1858", lon="11.6181", level="480,85")],
            nid=[nid_entry("16704", situation="sehr niedrig", klasse=2)],
        )
        use_upstream(monkeypatch, mock)

        stations = {s.number: s for s in await groundwater.fetch_stations()}

        assert set(stations) == {"16704", "16806"}
        assert stations["16704"].tier == "upper"
        assert stations["16806"].tier == "deep"
        assert (stations["16704"].situation, stations["16704"].situation_class) == (
            "sehr niedrig",
            2,
        )
        # A station the NID does not rate simply has no classification.
        assert stations["16806"].situation is None
        assert stations["16806"].situation_class is None

    @pytest.mark.anyio
    async def test_only_stations_inside_the_radius_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(
            upper=[
                gkd_entry("nah", lat=str(groundwater.MUNICH_LAT + 0.4)),
                gkd_entry("fern", lat=str(groundwater.MUNICH_LAT + 0.5)),
            ]
        )
        use_upstream(monkeypatch, mock)

        stations = await groundwater.fetch_stations()

        assert [s.number for s in stations] == ["nah"]

    @pytest.mark.anyio
    async def test_the_user_agent_identifies_the_add_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The GKD is a public authority's server; it has to be able to see
        # who is asking and how to get hold of us (same rule as MET Norway).
        mock = MockUpstream(upper=[gkd_entry()], nid=[nid_entry()])
        use_upstream(monkeypatch, mock)

        await groundwater.fetch_stations()

        assert mock.gkd_requests and mock.nid_requests
        for request in mock.requests:
            assert "Familienkalender" in request.headers["User-Agent"]
            assert "github.com/roland1910/Familienkalender" in request.headers["User-Agent"]

    @pytest.mark.anyio
    async def test_an_unreachable_service_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))

        with pytest.raises(groundwater.GroundwaterUnavailableError) as excinfo:
            await groundwater.fetch_stations()

        assert "nicht erreichbar" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_an_http_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        use_upstream(monkeypatch, MockUpstream(status=503))

        with pytest.raises(groundwater.GroundwaterUnavailableError) as excinfo:
            await groundwater.fetch_stations()

        assert "503" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_a_page_without_any_station_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not an empty result — "the pages changed" is a failure, and the
        # protection rule turns it into "keep what we have".
        use_upstream(monkeypatch, MockUpstream(body="<html>Wartung</html>"))

        with pytest.raises(groundwater.GroundwaterUnavailableError):
            await groundwater.fetch_stations()

    @pytest.mark.anyio
    async def test_the_size_limit_holds_without_a_content_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(upper=[gkd_entry()], stream=True)
        use_upstream(monkeypatch, mock)
        monkeypatch.setattr(groundwater, "MAX_PAGE_BYTES", 50)

        with pytest.raises(groundwater.GroundwaterUnavailableError) as excinfo:
            await groundwater.fetch_stations()

        assert "zu groß" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_a_streamed_page_is_parsed_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry()], stream=True))

        stations = await groundwater.fetch_stations()

        assert [s.number for s in stations] == ["16704"]

    @pytest.mark.anyio
    async def test_a_failing_nid_never_costs_the_stations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The low-water rating is an extra, not the core of the view.
        mock = MockUpstream(upper=[gkd_entry()])
        use_upstream(monkeypatch, mock)

        def failing() -> httpx.AsyncClient:
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("nid down")

            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(groundwater, "create_nid_client", failing)

        [station] = await groundwater.fetch_stations()

        assert station.number == "16704"
        assert station.situation is None


class TestRefreshStations:
    @pytest.mark.anyio
    async def test_a_successful_run_stores_the_stations_and_a_status(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry()]))

        stored = await groundwater.refresh_stations(storage, now=NOW)

        assert stored == 1
        assert [s.number for s in storage.list_groundwater_stations()] == ["16704"]
        status = settings_status(storage)
        assert status["stations"] == 1
        assert status["error"] is None
        assert status["last_run"] == NOW.isoformat()

    @pytest.mark.anyio
    async def test_a_second_run_on_the_same_day_does_not_refetch(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        mock = MockUpstream(upper=[gkd_entry()])
        use_upstream(monkeypatch, mock)
        await groundwater.refresh_stations(storage, now=NOW)
        before = len(mock.gkd_requests)

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(hours=5))

        assert len(mock.gkd_requests) == before

    @pytest.mark.anyio
    async def test_the_next_day_refetches(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        mock = MockUpstream(upper=[gkd_entry()])
        use_upstream(monkeypatch, mock)
        await groundwater.refresh_stations(storage, now=NOW)
        before = len(mock.gkd_requests)

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(hours=25))

        assert len(mock.gkd_requests) > before

    @pytest.mark.anyio
    async def test_a_failed_run_keeps_the_previous_stations(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        # THE protection rule: a broken fetch must never blank the map.
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry()]))
        await groundwater.refresh_stations(storage, now=NOW)
        use_upstream(monkeypatch, MockUpstream(down=True))

        stored = await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(days=2))

        assert stored == 1
        assert [s.number for s in storage.list_groundwater_stations()] == ["16704"]
        assert "nicht erreichbar" in settings_status(storage)["error"]

    @pytest.mark.anyio
    async def test_an_empty_result_keeps_the_previous_stations(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry()]))
        await groundwater.refresh_stations(storage, now=NOW)
        use_upstream(monkeypatch, MockUpstream(body=pointer_page([])))

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(days=2))

        assert [s.number for s in storage.list_groundwater_stations()] == ["16704"]

    @pytest.mark.anyio
    async def test_a_refresh_never_touches_the_readings(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        storage.upsert_groundwater_readings("16704", [("2026-09-19", 508.98)])
        use_upstream(monkeypatch, MockUpstream(upper=[gkd_entry()]))

        await groundwater.refresh_stations(storage, now=NOW)

        assert storage.list_groundwater_readings("16704") == [("2026-09-19", 508.98)]

    @pytest.mark.anyio
    async def test_a_failure_is_retried_after_an_hour_not_immediately(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        mock = MockUpstream(down=True)
        use_upstream(monkeypatch, mock)
        await groundwater.refresh_stations(storage, now=NOW)
        after_first = len(mock.gkd_requests)

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(minutes=5))
        assert len(mock.gkd_requests) == after_first

        await groundwater.refresh_stations(storage, now=NOW + dt.timedelta(minutes=61))
        assert len(mock.gkd_requests) > after_first

    @pytest.mark.anyio
    async def test_the_error_message_is_sanitized_and_bounded(
        self, monkeypatch: pytest.MonkeyPatch, storage: Storage
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))

        await groundwater.refresh_stations(storage, now=NOW)

        error = settings_status(storage)["error"]
        assert "@" not in error
        assert len(error) <= 500


def settings_status(storage: Storage) -> dict:
    from app.settings import get_groundwater_status

    return get_groundwater_status(storage)


def seed(storage: Storage, *stations: GroundwaterStation) -> None:
    storage.replace_groundwater_stations(list(stations))


def make_station(
    number: str = "16704",
    *,
    measured_at: str | None = None,
    tier: str = "upper",
    situation: str | None = None,
    situation_class: int | None = None,
) -> GroundwaterStation:
    return GroundwaterStation(
        number=number,
        name=f"Messstelle {number}",
        lat=48.1288,
        lon=11.5863,
        tier=tier,
        aquifer="Quartär",
        level_m_nn=508.98,
        depth_m=4.28,
        measured_at=measured_at
        or (dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)).isoformat(),
        uri="https://www.gkd.bayern.de/de/grundwasser/oberesstockwerk/x/messwerte",
        situation=situation,
        situation_class=situation_class,
    )


class TestStationsEndpoint:
    def test_serves_the_stored_stations(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))
        seed(api_storage(tmp_path), make_station(situation="niedrig", situation_class=1))

        response = client.get("/api/groundwater/stations")

        assert response.status_code == 200
        [station] = response.json()["stations"]
        assert station["number"] == "16704"
        assert station["name"] == "Messstelle 16704"
        assert station["lat"] == pytest.approx(48.1288)
        assert station["lon"] == pytest.approx(11.5863)
        assert station["tier"] == "upper"
        assert station["aquifer"] == "Quartär"
        assert station["level_m_nn"] == pytest.approx(508.98)
        assert station["depth_m"] == pytest.approx(4.28)
        assert station["situation"] == "niedrig"
        assert station["situation_class"] == 1
        assert station["stale"] is False
        assert isinstance(station["measured_at"], int)

    def test_measured_at_is_epoch_milliseconds(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))
        moment = dt.datetime(2026, 9, 19, 8, 0, tzinfo=dt.UTC)
        seed(api_storage(tmp_path), make_station(measured_at=moment.isoformat()))

        [station] = client.get("/api/groundwater/stations").json()["stations"]

        assert station["measured_at"] == int(moment.timestamp() * 1000)

    def test_stale_stations_are_withheld_by_default(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bavaria-wide there are stations whose last reading is from 1947 —
        # those must never show up as a dot on the map.
        use_upstream(monkeypatch, MockUpstream(down=True))
        seed(
            api_storage(tmp_path),
            make_station("aktuell"),
            make_station("1947", measured_at="1947-04-19T11:00:00+00:00"),
        )

        [station] = client.get("/api/groundwater/stations").json()["stations"]

        assert station["number"] == "aktuell"

    def test_stale_stations_can_be_requested_explicitly(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))
        seed(
            api_storage(tmp_path),
            make_station("aktuell"),
            make_station("1947", measured_at="1947-04-19T11:00:00+00:00"),
        )

        stations = client.get(
            "/api/groundwater/stations", params={"stale": "true"}
        ).json()["stations"]

        assert {s["number"]: s["stale"] for s in stations} == {
            "aktuell": False,
            "1947": True,
        }

    def test_a_station_without_a_timestamp_counts_as_stale(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))
        seed(api_storage(tmp_path), make_station("ohne", measured_at="--"))

        assert client.get("/api/groundwater/stations").json()["stations"] == []

    def test_an_empty_database_is_filled_on_first_request(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(upper=[gkd_entry(measured=_recent())])
        use_upstream(monkeypatch, mock)

        [station] = client.get("/api/groundwater/stations").json()["stations"]

        assert station["number"] == "16704"
        assert api_storage(tmp_path).count_groundwater_stations() == 1

    def test_a_second_request_is_served_from_the_database(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(upper=[gkd_entry(measured=_recent())])
        use_upstream(monkeypatch, mock)
        client.get("/api/groundwater/stations")
        after_first = len(mock.gkd_requests)

        client.get("/api/groundwater/stations")

        assert len(mock.gkd_requests) == after_first
        assert after_first == 2  # one page per tier

    def test_an_unreachable_source_without_any_data_is_a_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))

        response = client.get("/api/groundwater/stations")

        assert response.status_code == 502
        assert "erreichbar" in response.json()["detail"]

    def test_an_unreachable_source_still_serves_what_we_have(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed(api_storage(tmp_path), make_station())
        use_upstream(monkeypatch, MockUpstream(down=True))

        response = client.get("/api/groundwater/stations")

        assert response.status_code == 200
        assert len(response.json()["stations"]) == 1

    def test_the_answer_is_not_cached_by_the_browser(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(down=True))
        seed(api_storage(tmp_path), make_station())

        response = client.get("/api/groundwater/stations")

        assert response.headers["Cache-Control"] == "no-store"


def _recent() -> str:
    """A GKD timestamp a few hours old (so the station is not stale)."""
    moment = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    return moment.strftime("%d.%m.%Y %H:%M")


class TestHistoryEndpoint:
    def test_returns_the_stored_daily_levels(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"))
        store.upsert_groundwater_readings(
            "16704", [("2026-09-19", 508.98), ("2026-09-18", 509.10)]
        )

        payload = client.get("/api/groundwater/history/16704").json()

        assert payload["number"] == "16704"
        assert [point["v"] for point in payload["points"]] == [509.10, 508.98]
        assert payload["points"][0]["t"] == int(
            dt.datetime(2026, 9, 18, tzinfo=dt.UTC).timestamp() * 1000
        )

    def test_a_station_without_history_yields_an_empty_series(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        seed(api_storage(tmp_path), make_station("16704"))

        payload = client.get("/api/groundwater/history/16704").json()

        assert payload == {"number": "16704", "points": []}

    def test_an_unknown_station_is_a_404(self, client: TestClient, tmp_path: Path) -> None:
        seed(api_storage(tmp_path), make_station("16704"))

        assert client.get("/api/groundwater/history/99999").status_code == 404

    @pytest.mark.parametrize(
        "number",
        [
            "abc",
            "-5",
            "+12",
            "1_2",
            " 12",
            "12 ",
            "1.2",
            "1e3",
            "\uff10\uff11\uff12",  # full-width digits: int() would accept these
            "9" * 13,
        ],
    )
    def test_a_non_numeric_station_number_is_rejected(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: str
    ) -> None:
        # The number comes from the client. It is parsed as a strict digit
        # string (int() alone would swallow " 12", "+12" and even "1_2") and
        # only ever looked up in our own database — it never reaches a URL.
        mock = MockUpstream(down=True)
        use_upstream(monkeypatch, mock)
        seed(api_storage(tmp_path), make_station("16704"))

        response = client.get(f"/api/groundwater/history/{number}")

        assert response.status_code == 400
        assert response.json()["detail"] == "Ungültige Messstellennummer."
        assert mock.requests == []


def _day(offset: int) -> str:
    """An ISO day ``offset`` days before today (the window is relative)."""
    return (dt.date.today() - dt.timedelta(days=offset)).isoformat()


class TestSparklinesEndpoint:
    """One collected answer for all stations — 164 single history calls when
    the view opens would be absurd."""

    def test_returns_bare_value_lists_oldest_first(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"), make_station("16705"))
        store.upsert_groundwater_readings(
            "16704", [(_day(3), 508.9), (_day(1), 509.3), (_day(2), 509.1)]
        )
        store.upsert_groundwater_readings("16705", [(_day(1), 404.5)])

        payload = client.get("/api/groundwater/sparklines").json()

        assert payload["days"] == groundwater.SPARKLINE_DEFAULT_DAYS
        # Values only — a sparkline 60px wide has no room for timestamps.
        assert payload["series"]["16704"] == [508.9, 509.1, 509.3]
        assert payload["series"]["16705"] == [404.5]

    def test_a_station_without_history_is_simply_absent(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"), make_station("16705"))
        store.upsert_groundwater_readings("16704", [(_day(1), 508.9)])

        series = client.get("/api/groundwater/sparklines").json()["series"]

        assert set(series) == {"16704"}

    def test_long_series_are_thinned_but_keep_their_ends(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"))
        # One year of daily values — far more than a 60px sparkline can show.
        readings = [(_day(offset), 500.0 + offset) for offset in range(364, -1, -1)]
        store.upsert_groundwater_readings("16704", readings)

        values = client.get(
            "/api/groundwater/sparklines", params={"days": "365"}
        ).json()["series"]["16704"]

        assert len(values) == groundwater.MAX_SPARKLINE_POINTS
        # Order preserving, and both ends of the period survive the thinning.
        assert values == sorted(values, reverse=True)
        assert values[0] == pytest.approx(500.0 + 364)
        assert values[-1] == pytest.approx(500.0)

    def test_short_series_pass_through_untouched(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"))
        readings = [(_day(offset), 500.0 + offset) for offset in range(9, -1, -1)]
        store.upsert_groundwater_readings("16704", readings)

        values = client.get("/api/groundwater/sparklines").json()["series"]["16704"]

        assert len(values) == 10

    def test_only_readings_inside_the_window_are_returned(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        store = api_storage(tmp_path)
        seed(store, make_station("16704"))
        store.upsert_groundwater_readings(
            "16704", [(_day(200), 500.0), (_day(29), 501.0), (_day(1), 502.0)]
        )

        values = client.get(
            "/api/groundwater/sparklines", params={"days": "30"}
        ).json()["series"]["16704"]

        assert values == [501.0, 502.0]

    @pytest.mark.parametrize("raw", ["0", "-5", "7", "99999", "abc", "", "1e3", "90.5"])
    def test_an_unexpected_days_value_falls_back_to_the_default(
        self, client: TestClient, tmp_path: Path, raw: str
    ) -> None:
        seed(api_storage(tmp_path), make_station("16704"))

        payload = client.get("/api/groundwater/sparklines", params={"days": raw}).json()

        assert payload["days"] == groundwater.SPARKLINE_DEFAULT_DAYS

    @pytest.mark.parametrize("days", groundwater.SPARKLINE_ALLOWED_DAYS)
    def test_every_allowed_window_is_kept(
        self, client: TestClient, tmp_path: Path, days: int
    ) -> None:
        seed(api_storage(tmp_path), make_station("16704"))

        payload = client.get(
            "/api/groundwater/sparklines", params={"days": str(days)}
        ).json()

        assert payload["days"] == days

    def test_nothing_is_scraped_for_a_sparkline_request(
        self, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Purely a database read: the view opens with one stations call and
        # one sparkline call, neither of which may hit the GKD.
        mock = MockUpstream(down=True)
        use_upstream(monkeypatch, mock)
        seed(api_storage(tmp_path), make_station("16704"))

        assert client.get("/api/groundwater/sparklines").status_code == 200
        assert mock.requests == []
