// Unit tests for the groundwater marker placement. The markers have to sit
// on the very same pixel grid as the proxied base map tiles, and every tile
// the view asks for must stay inside the window the backend proxy accepts
// (app/weather.py: ALLOWED_ZOOMS and MAX_TILE_RADIUS around Munich).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CARD_GAP,
  CARD_HEIGHT,
  CARD_WIDTH,
  DEFAULT_GROUNDWATER_ZOOM,
  DOT_RADIUS,
  GROUNDWATER_ZOOMS,
  MAP_TILE_PX,
  MAX_MAP_PX,
  mapSize,
  markerPixel,
  munichPixel,
  STACK_GAP,
  stationMarkers,
  stepGroundwaterZoom,
} from "../../app/static/js/groundwater-map.js";
import {
  MUNICH_LAT,
  MUNICH_LON,
  projectPixel,
  viewportTiles,
} from "../../app/static/js/weather-map.js";

// Backend-side limits, duplicated on purpose so a one-sided change fails
// here instead of showing empty tiles (or 400s) on the kiosk.
const BACKEND_ZOOMS = [5, 6, 7, 8, 9, 10];
const BACKEND_TILE_RADIUS = 4;

// The radius of the station list (app/groundwater.py: RADIUS_KM).
const RADIUS_KM = 50;

const VIEW = { width: 900, height: 900 };

function station(number, lat, lon) {
  return { number, name: `Messstelle ${number}`, lat, lon };
}

function markers(stations, zoom = DEFAULT_GROUNDWATER_ZOOM, view = VIEW) {
  return stationMarkers(stations, zoom, MAP_TILE_PX, view.width, view.height);
}

test("Munich lands exactly in the middle of the viewport", () => {
  for (const zoom of GROUNDWATER_ZOOMS) {
    const point = munichPixel(zoom, MAP_TILE_PX, VIEW.width, VIEW.height);
    assert.ok(Math.abs(point.x - VIEW.width / 2) < 1e-6, `x off centre at zoom ${zoom}`);
    assert.ok(Math.abs(point.y - VIEW.height / 2) < 1e-6, `y off centre at zoom ${zoom}`);
  }
});

test("a marker sits on the same pixel grid as the tiles under it", () => {
  // Take the tile that contains a station and check the station lands at
  // the right offset inside it — that is what "pixel accurate" means here.
  const lat = 48.25;
  const lon = 11.35;
  const zoom = DEFAULT_GROUNDWATER_ZOOM;
  const tiles = viewportTiles(zoom, MAP_TILE_PX, VIEW.width, VIEW.height);
  const world = projectPixel(lon, lat, zoom, MAP_TILE_PX);
  const host = tiles.find(
    (tile) =>
      tile.x === Math.floor(world.x / MAP_TILE_PX) && tile.y === Math.floor(world.y / MAP_TILE_PX),
  );
  assert.ok(host !== undefined, "the station's own tile is not in the viewport");

  const marker = markerPixel(lon, lat, zoom, MAP_TILE_PX, VIEW.width, VIEW.height);
  assert.ok(Math.abs(marker.x - (host.left + (world.x - host.x * MAP_TILE_PX))) < 1e-6);
  assert.ok(Math.abs(marker.y - (host.top + (world.y - host.y * MAP_TILE_PX))) < 1e-6);
});

test("north is up and east is right", () => {
  const zoom = DEFAULT_GROUNDWATER_ZOOM;
  const centre = munichPixel(zoom, MAP_TILE_PX, VIEW.width, VIEW.height);
  const north = markerPixel(MUNICH_LON, MUNICH_LAT + 0.2, zoom, MAP_TILE_PX, 900, 900);
  const east = markerPixel(MUNICH_LON + 0.2, MUNICH_LAT, zoom, MAP_TILE_PX, 900, 900);
  assert.ok(north.y < centre.y, "north must be above the centre");
  assert.ok(east.x > centre.x, "east must be right of the centre");
});

test("the card sits above the dot and is centred on it", () => {
  const [marker] = markers([station("1", MUNICH_LAT, MUNICH_LON)]);
  assert.equal(marker.cardX, marker.x - CARD_WIDTH / 2);
  assert.equal(marker.cardY, marker.y - DOT_RADIUS - CARD_GAP - CARD_HEIGHT);
  assert.ok(marker.cardY + CARD_HEIGHT < marker.y, "the card must not cover the dot");
});

test("two stations on identical coordinates are both reachable", () => {
  // Obermenzing T 3 F (upper) and T 3 T (deeper aquifer) share a well head.
  const both = markers([
    station("T3T", 48.171, 11.453),
    station("T3F", 48.171, 11.453),
  ]);

  assert.equal(both.length, 2, "both stations must be drawn");
  // Same true position — the dot never lies about where the well is.
  assert.equal(both[0].x, both[1].x);
  assert.equal(both[0].y, both[1].y);
  // Their cards are stacked, not overlapping, so each can be tapped.
  const byNumber = Object.fromEntries(both.map((marker) => [marker.number, marker]));
  assert.equal(byNumber.T3F.stackIndex, 0);
  assert.equal(byNumber.T3T.stackIndex, 1);
  assert.equal(byNumber.T3T.cardY, byNumber.T3F.cardY - CARD_HEIGHT - STACK_GAP);
  assert.ok(byNumber.T3T.cardY + CARD_HEIGHT <= byNumber.T3F.cardY);
  // …and each one knows about the other, so the popover can offer it.
  assert.deepEqual(byNumber.T3F.siblings, ["T3F", "T3T"]);
  assert.deepEqual(byNumber.T3T.siblings, ["T3F", "T3T"]);
});

test("the stacking order of identical coordinates is deterministic", () => {
  const forwards = markers([station("B", 48.2, 11.5), station("A", 48.2, 11.5)]);
  const backwards = markers([station("A", 48.2, 11.5), station("B", 48.2, 11.5)]);
  const stack = (list) =>
    Object.fromEntries(list.map((marker) => [marker.number, marker.stackIndex]));
  assert.deepEqual(stack(forwards), stack(backwards));
  assert.deepEqual(stack(forwards), { A: 0, B: 1 });
});

test("a lone station has no siblings and no stacking", () => {
  const [marker] = markers([station("1", 48.2, 11.5)]);
  assert.equal(marker.stackIndex, 0);
  assert.deepEqual(marker.siblings, []);
});

test("stations only a few metres apart still get their own card", () => {
  // Not the same coordinates, so no stacking — they simply overlap a bit,
  // which is honest: they really are next to each other.
  const both = markers([station("1", 48.2, 11.5), station("2", 48.2001, 11.5)]);
  assert.equal(both.length, 2);
  assert.ok(both.every((marker) => marker.stackIndex === 0));
  assert.ok(both.every((marker) => marker.siblings.length === 0));
});

test("markers are ordered so southern ones paint on top", () => {
  const list = markers([
    station("sued", 47.9, 11.5),
    station("nord", 48.4, 11.5),
    station("mitte", 48.1374, 11.5755),
  ]);
  assert.deepEqual(
    list.map((marker) => marker.number),
    ["nord", "mitte", "sued"],
  );
});

test("stations far outside the viewport are dropped", () => {
  const list = markers([station("weit", 49.9, 13.9), station("nah", MUNICH_LAT, MUNICH_LON)]);
  assert.deepEqual(
    list.map((marker) => marker.number),
    ["nah"],
  );
});

test("unusable entries never produce a marker", () => {
  const list = markers([
    station("1", MUNICH_LAT, MUNICH_LON),
    { number: "2", lat: null, lon: 11.5 },
    { number: "3", lat: 48.1, lon: Number.NaN },
    { number: "", lat: 48.1, lon: 11.5 },
    { lat: 48.1, lon: 11.5 },
    null,
    "nonsense",
  ]);
  assert.deepEqual(
    list.map((marker) => marker.number),
    ["1"],
  );
});

test("a missing or broken station list yields no markers", () => {
  assert.deepEqual(markers([]), []);
  assert.deepEqual(markers(undefined), []);
  assert.deepEqual(markers(null), []);
});

test("the zoom buttons clamp at both ends", () => {
  assert.equal(stepGroundwaterZoom(8, -1), 8);
  assert.equal(stepGroundwaterZoom(8, 1), 9);
  assert.equal(stepGroundwaterZoom(10, 1), 10);
  assert.equal(stepGroundwaterZoom(10, -1), 9);
  // An unknown stored zoom starts from the default instead of breaking.
  assert.equal(stepGroundwaterZoom(99, 1), 10);
  assert.equal(stepGroundwaterZoom(99, -1), 8);
});

test("the rendered map is square and capped", () => {
  assert.equal(mapSize(900), 900);
  assert.equal(mapSize(4000), MAX_MAP_PX);
  assert.equal(mapSize(-10), 0);
  assert.equal(mapSize(640.7), 640);
});

test("every tile the map asks for is inside the window the backend proxies", () => {
  // Including the largest map the cap allows — a 4K screen must not walk
  // out of the proxy's tile window and collect 400s.
  const sizes = [MAX_MAP_PX, 900, 420, 320];
  for (const size of sizes) {
    for (const zoom of GROUNDWATER_ZOOMS) {
      assert.ok(BACKEND_ZOOMS.includes(zoom), `zoom ${zoom} not allowed by the proxy`);
      const centre = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, MAP_TILE_PX);
      const centreTile = {
        x: Math.floor(centre.x / MAP_TILE_PX),
        y: Math.floor(centre.y / MAP_TILE_PX),
      };
      for (const tile of viewportTiles(zoom, MAP_TILE_PX, size, size)) {
        assert.ok(
          Math.abs(tile.x - centreTile.x) <= BACKEND_TILE_RADIUS,
          `x out of range at zoom ${zoom}, size ${size}`,
        );
        assert.ok(
          Math.abs(tile.y - centreTile.y) <= BACKEND_TILE_RADIUS,
          `y out of range at zoom ${zoom}, size ${size}`,
        );
      }
    }
  }
});

test("the default zoom shows the whole 50 km radius on a kiosk map", () => {
  // Otherwise stations at the edge of the list would simply be invisible
  // until Roland zooms out, without anything telling him they exist.
  const size = 900;
  const dLat = RADIUS_KM / 111.0;
  const dLon = RADIUS_KM / (111.0 * Math.cos((MUNICH_LAT * Math.PI) / 180));
  const corners = [
    [MUNICH_LAT + dLat, MUNICH_LON],
    [MUNICH_LAT - dLat, MUNICH_LON],
    [MUNICH_LAT, MUNICH_LON + dLon],
    [MUNICH_LAT, MUNICH_LON - dLon],
  ];
  for (const [lat, lon] of corners) {
    const point = markerPixel(lon, lat, DEFAULT_GROUNDWATER_ZOOM, MAP_TILE_PX, size, size);
    assert.ok(point.x >= 0 && point.x <= size, `x ${point.x} outside the map`);
    assert.ok(point.y >= 0 && point.y <= size, `y ${point.y} outside the map`);
  }
});
