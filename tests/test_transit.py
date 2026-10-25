"""Tests for the MVG departure board (Etappe 52).

The MVG endpoint is UNOFFICIAL and undocumented, so every parse step here
is defensive in the same way as the groundwater scraper: a changed payload
must cost at most the departure board, never the view around it.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import transit
from app.main import app

client = TestClient(app, client=("127.0.0.1", 50000))

# Epoch ms, far enough apart to be unambiguous in assertions.
T0 = 1_792_935_000_000
MINUTE = 60_000


def _dep(
    label: str,
    direction: str,
    destination: str,
    *,
    minutes: int,
    delay: int | None = None,
    realtime: bool = True,
    cancelled: bool = False,
    kind: str = "BUS",
    planned_only: bool = False,
) -> dict:
    """One departure as the MVG v3 endpoint returns it."""
    planned = T0 + minutes * MINUTE
    entry: dict = {
        "plannedDepartureTime": planned,
        "transportType": kind,
        "label": label,
        "destination": destination,
        "cancelled": cancelled,
        "realtime": realtime,
        "delayInMinutes": delay,
        # The direction letter lives in the fourth lineId segment; it is the
        # only stable direction marker the payload carries (destination
        # names vary — "Olympia-Einkaufsz." vs "Olympia-Einkaufszentrum").
        "lineId": f"swm:03{label}:G:{direction}:016",
    }
    if not planned_only:
        entry["realtimeDepartureTime"] = planned + (delay or 0) * MINUTE
    return entry


class MockMVG:
    """Mocked MVG endpoint: a departure list per stop, plus a call counter."""

    def __init__(self, by_stop: dict[str, list[dict]], *, status: int = 200) -> None:
        self.by_stop = by_stop
        self.status = status
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        stop = request.url.params.get("globalId", "")
        self.calls.append(stop)
        if self.status != 200:
            return httpx.Response(self.status, text="upstream kaputt")
        return httpx.Response(200, json=self.by_stop.get(stop, []))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def create_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handler),
                base_url=transit.MVG_BASE_URL,
            )

        monkeypatch.setattr(transit, "create_mvg_client", create_client)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    transit.reset_caches()


def _all_stops(deps: list[dict]) -> dict[str, list[dict]]:
    return {transit.STOP_ALTE_ALLEE: deps, transit.STOP_LANGWIED: deps}


class TestParsingAndFiltering:
    def test_a_block_keeps_only_its_line_and_direction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    _dep("56", "H", "Fürstenried West", minutes=3),
                    _dep("56", "R", "Schloss Blutenburg", minutes=4),
                    _dep("143", "H", "Olympia-Einkaufsz.", minutes=5),
                    _dep("56", "H", "Fürstenried West", minutes=9),
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        groups = {g["key"]: g for g in client.get("/api/transit/departures").json()["groups"]}
        towards_pasing = groups["bus56_pasing"]["departures"]
        assert [d["destination"] for d in towards_pasing] == [
            "Fürstenried West",
            "Fürstenried West",
        ]
        assert [d["time"] for d in towards_pasing] == [T0 + 3 * MINUTE, T0 + 9 * MINUTE]

    def test_at_most_two_departures_per_block_in_time_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    _dep("56", "H", "Fürstenried West", minutes=20),
                    _dep("56", "H", "Fürstenried West", minutes=4),
                    _dep("56", "H", "Fürstenried West", minutes=11),
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        deps = next(
            g for g in client.get("/api/transit/departures").json()["groups"]
            if g["key"] == "bus56_pasing"
        )["departures"]
        assert [d["time"] for d in deps] == [T0 + 4 * MINUTE, T0 + 11 * MINUTE]

    def test_the_live_time_wins_over_the_timetable_but_both_are_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The display shows when the bus really comes; the delay is what
        makes that trustworthy, so the planned time travels with it."""
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    _dep("56", "H", "Fürstenried West", minutes=5, delay=3)
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        dep = next(
            g for g in client.get("/api/transit/departures").json()["groups"]
            if g["key"] == "bus56_pasing"
        )["departures"][0]
        assert dep["time"] == T0 + 8 * MINUTE
        assert dep["planned"] == T0 + 5 * MINUTE
        assert dep["delay"] == 3
        assert dep["realtime"] is True

    def test_without_a_live_time_the_timetable_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    _dep(
                        "56", "H", "Fürstenried West",
                        minutes=6, realtime=False, planned_only=True,
                    )
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        dep = next(
            g for g in client.get("/api/transit/departures").json()["groups"]
            if g["key"] == "bus56_pasing"
        )["departures"][0]
        assert dep["time"] == T0 + 6 * MINUTE
        assert dep["realtime"] is False

    def test_a_cancelled_run_is_shown_not_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dropping it silently would leave the board promising a bus that
        is not coming — the one case where the board must speak up."""
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    _dep("56", "H", "Fürstenried West", minutes=4, cancelled=True),
                    _dep("56", "H", "Fürstenried West", minutes=14),
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        deps = next(
            g for g in client.get("/api/transit/departures").json()["groups"]
            if g["key"] == "bus56_pasing"
        )["departures"]
        assert [d["cancelled"] for d in deps] == [True, False]

    def test_broken_entries_are_skipped_one_by_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A changed payload must cost single rows, never the whole board."""
        good = _dep("56", "H", "Fürstenried West", minutes=7)
        mock = MockMVG(
            {
                transit.STOP_ALTE_ALLEE: [
                    {},                                   # empty
                    {"label": "56", "lineId": "a:b:c:H"},  # no time at all
                    {"label": "56", "lineId": "nonsense", "plannedDepartureTime": T0},
                    {"label": "56", "lineId": "x:y:z:H", "plannedDepartureTime": "bald"},
                    "kein dict",                          # not even an object
                    good,
                ],
                transit.STOP_LANGWIED: [],
            }
        )
        mock.install(monkeypatch)

        response = client.get("/api/transit/departures")
        assert response.status_code == 200
        deps = next(
            g for g in response.json()["groups"] if g["key"] == "bus56_pasing"
        )["departures"]
        assert [d["time"] for d in deps] == [T0 + 7 * MINUTE]

    def test_a_payload_that_is_not_a_list_yields_an_empty_board(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMVG({})
        mock.by_stop = {}

        def handler(request: httpx.Request) -> httpx.Response:
            mock.calls.append("x")
            return httpx.Response(200, json={"error": "nope"})

        monkeypatch.setattr(
            transit,
            "create_mvg_client",
            lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=transit.MVG_BASE_URL
            ),
        )
        response = client.get("/api/transit/departures")
        assert response.status_code == 200
        assert all(g["departures"] == [] for g in response.json()["groups"])


class TestBoardShape:
    def test_every_configured_block_is_present_with_its_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        MockMVG(_all_stops([])).install(monkeypatch)
        groups = client.get("/api/transit/departures").json()["groups"]
        assert [g["key"] for g in groups] == [b.key for b in transit.BLOCKS]
        assert {g["side"] for g in groups} == {"left", "right"}
        for group in groups:
            assert group["label"]
            assert group["kind"] in {"bus", "sbahn"}

    def test_one_request_per_stop_not_per_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Four blocks share two stops; asking four times would be rude to
        an unofficial endpoint and slower for no gain."""
        mock = MockMVG(_all_stops([]))
        mock.install(monkeypatch)
        client.get("/api/transit/departures")
        assert sorted(mock.calls) == sorted({b.stop_id for b in transit.BLOCKS})


class TestCacheAndErrors:
    def test_a_second_call_inside_the_ttl_does_not_hit_the_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockMVG(_all_stops([_dep("56", "H", "Fürstenried West", minutes=3)]))
        mock.install(monkeypatch)
        client.get("/api/transit/departures")
        first = len(mock.calls)
        client.get("/api/transit/departures")
        assert len(mock.calls) == first

    def test_after_the_ttl_it_asks_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MockMVG(_all_stops([]))
        mock.install(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(transit, "_now", lambda: clock[0])
        client.get("/api/transit/departures")
        first = len(mock.calls)
        clock[0] += transit.DEPARTURES_CACHE_TTL_SECONDS + 1
        client.get("/api/transit/departures")
        assert len(mock.calls) > first

    def test_an_upstream_error_is_a_german_502(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        MockMVG({}, status=503).install(monkeypatch)
        response = client.get("/api/transit/departures")
        assert response.status_code == 502
        assert "Abfahrt" in response.json()["detail"]

    def test_an_unreachable_endpoint_is_a_german_502(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(
            transit,
            "create_mvg_client",
            lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=transit.MVG_BASE_URL
            ),
        )
        response = client.get("/api/transit/departures")
        assert response.status_code == 502
        assert "erreichbar" in response.json()["detail"].lower()

    def test_an_oversized_body_is_refused_even_without_content_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing or lying Content-Length must not bypass the cap."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"[" + b" " * 4096 + b"]"),
                headers={"Content-Type": "application/json"},
            )

        monkeypatch.setattr(
            transit,
            "create_mvg_client",
            lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url=transit.MVG_BASE_URL
            ),
        )
        monkeypatch.setattr(transit, "MAX_RESPONSE_BYTES", 64)
        response = client.get("/api/transit/departures")
        assert response.status_code == 502
