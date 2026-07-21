"""Tests for the radar frame list and the map tile proxy.

The tile proxy is the security-sensitive part of the weather view: it
fetches from fixed upstream hosts on behalf of the browser. These tests
nail down that nothing about the outgoing URL can be steered from the
request — neither the host, nor the zoom/tile window, nor the radar frame
path (which must come from RainViewer's own frame list).
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import weather
from app.main import app

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _index_body(times: list[int]) -> dict:
    """A RainViewer weather-maps.json with the given past frame times."""
    return {
        "version": "2.0",
        "generated": times[-1] if times else 0,
        "host": "https://tilecache.rainviewer.com",
        "radar": {
            "past": [{"time": t, "path": f"/v2/radar/{t}"} for t in times],
            "nowcast": [],
        },
    }


class MockUpstream:
    """Mocked RainViewer index plus OSM/RainViewer tile hosts."""

    def __init__(
        self,
        index: dict | None = None,
        *,
        index_down: bool = False,
        index_status: int = 200,
        index_raw: bytes | None = None,
        tile_status: int = 200,
        tile_body: bytes = PNG_BYTES,
        tile_content_type: str = "image/png",
    ) -> None:
        self.index = index if index is not None else _index_body([1_700_000_000])
        self.index_down = index_down
        self.index_status = index_status
        self.index_raw = index_raw
        self.tile_status = tile_status
        self.tile_body = tile_body
        self.tile_content_type = tile_content_type
        self.index_requests = 0
        self.tile_requests: list[str] = []

    def index_handler(self, request: httpx.Request) -> httpx.Response:
        self.index_requests += 1
        assert "Familienkalender" in request.headers["User-Agent"]
        if self.index_down:
            raise httpx.ConnectError("connection refused")
        if self.index_status != 200:
            return httpx.Response(self.index_status)
        if self.index_raw is not None:
            return httpx.Response(200, content=self.index_raw)
        return httpx.Response(200, json=self.index)

    def tile_handler(self, request: httpx.Request) -> httpx.Response:
        self.tile_requests.append(str(request.url))
        assert "Familienkalender" in request.headers["User-Agent"]
        if self.tile_status != 200:
            return httpx.Response(self.tile_status)
        return httpx.Response(
            200, content=self.tile_body, headers={"Content-Type": self.tile_content_type}
        )

    def index_factory(self) -> Callable[[], httpx.AsyncClient]:
        def create_rainviewer_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.index_handler),
                headers={"User-Agent": weather.USER_AGENT},
            )

        return create_rainviewer_client

    def tile_factory(self) -> Callable[[str], httpx.AsyncClient]:
        def create_tile_client(base_url: str) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.tile_handler),
                base_url=base_url,
                headers={"User-Agent": weather.USER_AGENT},
            )

        return create_tile_client


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    weather.reset_caches()
    return TestClient(app, client=("127.0.0.1", 50000))


def use_upstream(monkeypatch: pytest.MonkeyPatch, mock: MockUpstream) -> None:
    monkeypatch.setattr(weather, "create_rainviewer_client", mock.index_factory())
    monkeypatch.setattr(weather, "create_tile_client", mock.tile_factory())


def munich(zoom: int) -> tuple[int, int]:
    return weather.munich_tile(zoom)


DEFAULT_ZOOM = weather.DEFAULT_ZOOM


class TestRadarFrames:
    def test_returns_the_recent_past_frames(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        times = [1_700_000_000 + i * 600 for i in range(13)]
        use_upstream(monkeypatch, MockUpstream(_index_body(times)))
        response = client.get("/api/weather/radar/frames")
        assert response.status_code == 200
        frames = response.json()["frames"]
        assert 0 < len(frames) <= weather.MAX_RADAR_FRAMES
        # Newest frames, oldest first (that is the animation order).
        assert [f["id"] for f in frames] == times[-len(frames) :]
        assert frames[0]["t"] == frames[0]["id"] * 1000

    def test_malformed_entries_are_skipped(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = _index_body([1_700_000_000, 1_700_000_600])
        body["radar"]["past"].extend(
            [
                {"time": "later", "path": "/v2/radar/x"},
                {"path": "/v2/radar/no-time"},
                {"time": 1_700_001_200},  # no path
                "nonsense",
            ]
        )
        use_upstream(monkeypatch, MockUpstream(body))
        frames = client.get("/api/weather/radar/frames").json()["frames"]
        assert [f["id"] for f in frames] == [1_700_000_000, 1_700_000_600]

    def test_empty_frame_list_is_an_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(_index_body([])))
        response = client.get("/api/weather/radar/frames")
        assert response.status_code == 502
        assert "Regenradar" in response.json()["detail"]

    def test_second_request_within_ttl_hits_the_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(_index_body([1_700_000_000]))
        use_upstream(monkeypatch, mock)
        client.get("/api/weather/radar/frames")
        client.get("/api/weather/radar/frames")
        assert mock.index_requests == 1

    def test_cache_expires_after_ttl(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(_index_body([1_700_000_000]))
        use_upstream(monkeypatch, mock)
        clock = {"now": 1000.0}
        monkeypatch.setattr(weather, "_now", lambda: clock["now"])
        client.get("/api/weather/radar/frames")
        clock["now"] += weather.RADAR_FRAMES_CACHE_TTL_SECONDS + 1
        client.get("/api/weather/radar/frames")
        assert mock.index_requests == 2

    def test_cache_ttl_is_about_five_minutes(self) -> None:
        assert 120 <= weather.RADAR_FRAMES_CACHE_TTL_SECONDS <= 600

    def test_service_down_is_502_with_german_message(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(index_down=True))
        response = client.get("/api/weather/radar/frames")
        assert response.status_code == 502
        assert response.json()["detail"] == "Der Regenradar-Dienst ist nicht erreichbar."

    def test_broken_json_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(index_raw=b"{oops"))
        response = client.get("/api/weather/radar/frames")
        assert response.status_code == 502


class TestBaseTileProxy:
    def test_serves_a_tile_from_openstreetmap(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream()
        use_upstream(monkeypatch, mock)
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 200
        assert response.content == PNG_BYTES
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
        # Tiles are exempt from the API-wide no-store (app.main
        # API_CACHE_EXEMPT_PREFIXES) — the kiosk re-requests them constantly.
        assert response.headers["cache-control"] != "no-store"
        assert "max-age" in response.headers["cache-control"]
        assert mock.tile_requests == [
            f"https://tile.openstreetmap.org/{DEFAULT_ZOOM}/{x}/{y}.png"
        ]

    def test_second_request_is_served_from_the_server_side_cache(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream()
        use_upstream(monkeypatch, mock)
        x, y = munich(DEFAULT_ZOOM)
        path = f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x}/{y}"
        client.get(path)
        client.get(path)
        assert len(mock.tile_requests) == 1

    def test_upstream_error_is_502(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_upstream(monkeypatch, MockUpstream(tile_status=500))
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 502
        assert "Kartendienst" in response.json()["detail"]

    def test_non_image_content_type_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Never relay something that is not an image — a proxied HTML body
        # would be same-origin content in the browser.
        use_upstream(
            monkeypatch,
            MockUpstream(tile_body=b"<html>hi</html>", tile_content_type="text/html"),
        )
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 502

    def test_oversized_tile_is_refused(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(weather, "MAX_TILE_BYTES", 16)
        use_upstream(monkeypatch, MockUpstream())
        x, y = munich(DEFAULT_ZOOM)
        assert client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x}/{y}").status_code == 502


class TestTileCoordinateValidation:
    """z/x/y are strictly integers inside the Munich window — nothing else."""

    @pytest.fixture(autouse=True)
    def _upstream(self, monkeypatch: pytest.MonkeyPatch) -> MockUpstream:
        mock = MockUpstream()
        use_upstream(monkeypatch, mock)
        self.mock = mock
        return mock

    def test_allowed_zooms_are_exactly_the_ones_the_frontend_uses(self) -> None:
        assert weather.ALLOWED_ZOOMS == (8, 9, 10)
        assert weather.DEFAULT_ZOOM in weather.ALLOWED_ZOOMS

    @pytest.mark.parametrize("zoom", [0, 1, 7, 11, 19, 25])
    def test_zoom_outside_the_allowlist_is_rejected(
        self, client: TestClient, zoom: int
    ) -> None:
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/base/{zoom}/{x}/{y}")
        assert response.status_code == 400
        assert self.mock.tile_requests == []

    def test_tiles_far_from_munich_are_rejected(self, client: TestClient) -> None:
        x, y = munich(DEFAULT_ZOOM)
        far = weather.MAX_TILE_RADIUS + 1
        for candidate in ((x + far, y), (x - far, y), (x, y + far), (x, y - far)):
            response = client.get(
                f"/api/weather/tile/base/{DEFAULT_ZOOM}/{candidate[0]}/{candidate[1]}"
            )
            assert response.status_code == 400, candidate
        assert self.mock.tile_requests == []

    def test_tiles_inside_the_window_are_accepted(self, client: TestClient) -> None:
        x, y = munich(DEFAULT_ZOOM)
        edge = weather.MAX_TILE_RADIUS
        response = client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/{x + edge}/{y - edge}")
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "coords",
        [
            ("abc", "1", "2"),
            ("9", "x", "2"),
            ("9", "1", "y"),
            ("9", "1e3", "2"),
            ("9", "0x10", "2"),
            ("9", "1.5", "2"),
            ("9", " 272", "177"),
            ("9", "+272", "177"),
            ("9", "", "177"),
        ],
    )
    def test_non_integer_coordinates_are_rejected(
        self, client: TestClient, coords: tuple[str, str, str]
    ) -> None:
        zoom, x, y = coords
        response = client.get(f"/api/weather/tile/base/{zoom}/{x}/{y}")
        assert response.status_code in (400, 404)
        assert self.mock.tile_requests == []

    def test_negative_coordinates_are_rejected(self, client: TestClient) -> None:
        response = client.get(f"/api/weather/tile/base/{DEFAULT_ZOOM}/-1/-1")
        assert response.status_code == 400
        assert self.mock.tile_requests == []

    def test_absurdly_large_coordinates_are_rejected(self, client: TestClient) -> None:
        response = client.get(
            f"/api/weather/tile/base/{DEFAULT_ZOOM}/99999999999999999999/1"
        )
        assert response.status_code == 400
        assert self.mock.tile_requests == []


class TestRadarTileProxy:
    def test_serves_a_radar_tile_for_a_known_frame(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = 1_700_000_000
        mock = MockUpstream(_index_body([frame]))
        use_upstream(monkeypatch, mock)
        client.get("/api/weather/radar/frames")
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/radar/{frame}/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 200
        assert response.content == PNG_BYTES
        assert mock.tile_requests == [
            f"https://tilecache.rainviewer.com/v2/radar/{frame}/256/{DEFAULT_ZOOM}/{x}/{y}/2/1_1.png"
        ]

    def test_frame_list_is_fetched_on_demand_when_not_cached(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = 1_700_000_000
        mock = MockUpstream(_index_body([frame]))
        use_upstream(monkeypatch, mock)
        x, y = munich(DEFAULT_ZOOM)
        # No preceding /radar/frames call: the proxy resolves the frame itself.
        response = client.get(f"/api/weather/tile/radar/{frame}/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 200
        assert mock.index_requests == 1

    def test_unknown_frame_id_is_404_and_never_reaches_upstream(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = MockUpstream(_index_body([1_700_000_000]))
        use_upstream(monkeypatch, mock)
        client.get("/api/weather/radar/frames")
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/radar/1234567890/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code == 404
        assert mock.tile_requests == []

    @pytest.mark.parametrize(
        "frame",
        ["..%2F..%2Fetc%2Fpasswd", "v2%2Fradar%2F1", "abc", "-1", "1_700_000_000"],
    )
    def test_non_numeric_frame_ids_never_reach_upstream(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, frame: str
    ) -> None:
        mock = MockUpstream(_index_body([1_700_000_000]))
        use_upstream(monkeypatch, mock)
        x, y = munich(DEFAULT_ZOOM)
        response = client.get(f"/api/weather/tile/radar/{frame}/{DEFAULT_ZOOM}/{x}/{y}")
        assert response.status_code in (400, 404)
        assert mock.tile_requests == []

    def test_radar_tile_coordinates_are_validated_too(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = 1_700_000_000
        mock = MockUpstream(_index_body([frame]))
        use_upstream(monkeypatch, mock)
        client.get("/api/weather/radar/frames")
        x, y = munich(DEFAULT_ZOOM)
        far = weather.MAX_TILE_RADIUS + 5
        response = client.get(f"/api/weather/tile/radar/{frame}/{DEFAULT_ZOOM}/{x + far}/{y}")
        assert response.status_code == 400
        assert mock.tile_requests == []

    def test_radar_tiles_are_cached_far_shorter_than_base_tiles(self) -> None:
        # Radar imagery goes stale within minutes; the base map does not.
        assert weather.RADAR_TILE_CACHE_TTL_SECONDS <= 600
        assert weather.BASE_TILE_CACHE_TTL_SECONDS >= 3600


class TestTileMath:
    def test_munich_tile_matches_the_slippy_map_formula(self) -> None:
        # Reference values from the standard OSM slippy-map tilenames.
        assert weather.munich_tile(8) == (136, 88)
        assert weather.munich_tile(9) == (272, 177)
        assert weather.munich_tile(10) == (544, 355)

    def test_tile_window_is_centred_on_munich(self) -> None:
        x, y = weather.munich_tile(weather.DEFAULT_ZOOM)
        assert weather.is_allowed_tile(weather.DEFAULT_ZOOM, x, y)
        assert weather.is_allowed_tile(
            weather.DEFAULT_ZOOM, x + weather.MAX_TILE_RADIUS, y + weather.MAX_TILE_RADIUS
        )
        assert not weather.is_allowed_tile(
            weather.DEFAULT_ZOOM, x + weather.MAX_TILE_RADIUS + 1, y
        )
