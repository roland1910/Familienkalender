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
  MAX_MAP_PX,
  mapSize,
  markerPixel,
  metresPerPixel,
  munichPixel,
  NATIVE_TILE_PX,
  OVERVIEW_DOT_RADIUS,
  showsSparklines,
  SPARKLINE_MIN_ZOOM,
  STACK_GAP,
  STATION_CIRCLE_FILL,
  STATION_RADIUS_KM,
  stationMarkers,
  stepGroundwaterZoom,
  TARGET_COVERAGE_M,
  tilePixelSize,
} from "../../app/static/js/groundwater-map.js";
import {
  MAX_TILES_PER_LAYER,
  MUNICH_LAT,
  MUNICH_LON,
  projectPixel,
  viewportTiles,
} from "../../app/static/js/weather-map.js";

// Backend-side limits, duplicated on purpose so a one-sided change fails
// here instead of showing empty tiles (or 400s) on the kiosk.
const BACKEND_ZOOMS = [5, 6, 7, 8, 9, 10, 11];
const BACKEND_TILE_RADIUS = 4;

// The radius of the station list (app/groundwater.py: RADIUS_KM).
const RADIUS_KM = 50;

// Bounding box of the 178 stations the live service actually returns
// (measured 2026-09-22). The 50 km radius is the guarantee, this is the
// reality the margin is judged against.
const STATION_BBOX = { latMin: 47.7124, latMax: 48.5115, lonMin: 10.9527, lonMax: 12.2367 };

const VIEW = { width: 900, height: 900 };

// The tile edge the view really renders at this map size — markers and
// tiles both take it from the map width, so the tests must too.
const VIEW_TILE_PX = tilePixelSize(VIEW.width);

function station(number, lat, lon) {
  return { number, name: `Messstelle ${number}`, lat, lon };
}

function markers(stations, zoom = DEFAULT_GROUNDWATER_ZOOM, view = VIEW) {
  return stationMarkers(stations, zoom, tilePixelSize(view.width), view.width, view.height);
}

test("Munich lands exactly in the middle of the viewport", () => {
  for (const zoom of GROUNDWATER_ZOOMS) {
    const point = munichPixel(zoom, VIEW_TILE_PX, VIEW.width, VIEW.height);
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
  const tiles = viewportTiles(zoom, VIEW_TILE_PX, VIEW.width, VIEW.height);
  const world = projectPixel(lon, lat, zoom, VIEW_TILE_PX);
  const host = tiles.find(
    (tile) =>
      tile.x === Math.floor(world.x / VIEW_TILE_PX) && tile.y === Math.floor(world.y / VIEW_TILE_PX),
  );
  assert.ok(host !== undefined, "the station's own tile is not in the viewport");

  const marker = markerPixel(lon, lat, zoom, VIEW_TILE_PX, VIEW.width, VIEW.height);
  assert.ok(Math.abs(marker.x - (host.left + (world.x - host.x * VIEW_TILE_PX))) < 1e-6);
  assert.ok(Math.abs(marker.y - (host.top + (world.y - host.y * VIEW_TILE_PX))) < 1e-6);
});

test("north is up and east is right", () => {
  const zoom = DEFAULT_GROUNDWATER_ZOOM;
  const centre = munichPixel(zoom, VIEW_TILE_PX, VIEW.width, VIEW.height);
  const north = markerPixel(MUNICH_LON, MUNICH_LAT + 0.2, zoom, VIEW_TILE_PX, 900, 900);
  const east = markerPixel(MUNICH_LON + 0.2, MUNICH_LAT, zoom, VIEW_TILE_PX, 900, 900);
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
  const widest = GROUNDWATER_ZOOMS[0];
  const closest = GROUNDWATER_ZOOMS[GROUNDWATER_ZOOMS.length - 1];
  assert.equal(stepGroundwaterZoom(widest, -1), widest);
  assert.equal(stepGroundwaterZoom(widest, 1), widest + 1);
  assert.equal(stepGroundwaterZoom(closest, 1), closest);
  assert.equal(stepGroundwaterZoom(closest, -1), closest - 1);
  // An unknown stored zoom starts from the default instead of breaking.
  assert.equal(stepGroundwaterZoom(99, 1), DEFAULT_GROUNDWATER_ZOOM + 1);
  assert.equal(stepGroundwaterZoom(99, -1), DEFAULT_GROUNDWATER_ZOOM - 1);
  // Every step lands on a level the buttons actually offer.
  for (const zoom of GROUNDWATER_ZOOMS) {
    assert.ok(GROUNDWATER_ZOOMS.includes(stepGroundwaterZoom(zoom, 1)));
    assert.ok(GROUNDWATER_ZOOMS.includes(stepGroundwaterZoom(zoom, -1)));
  }
});

test("the default is the middle level, so both buttons do something", () => {
  assert.ok(GROUNDWATER_ZOOMS.includes(DEFAULT_GROUNDWATER_ZOOM));
  assert.notEqual(DEFAULT_GROUNDWATER_ZOOM, GROUNDWATER_ZOOMS[0]);
  assert.notEqual(DEFAULT_GROUNDWATER_ZOOM, GROUNDWATER_ZOOMS[GROUNDWATER_ZOOMS.length - 1]);
});

test("the rendered map is square and capped", () => {
  assert.equal(mapSize(900), 900);
  assert.equal(mapSize(4000), MAX_MAP_PX);
  assert.equal(mapSize(-10), 0);
  assert.equal(mapSize(640.7), 640);
});

const MAP_SIZES = [MAX_MAP_PX, 900, 840, 420, 320];

test("every tile the map asks for is inside the window the backend proxies", () => {
  // Including the largest map the cap allows — a 4K screen must not walk
  // out of the proxy's tile window and collect 400s.
  for (const size of MAP_SIZES) {
    const tilePx = tilePixelSize(size);
    for (const zoom of GROUNDWATER_ZOOMS) {
      assert.ok(BACKEND_ZOOMS.includes(zoom), `zoom ${zoom} not allowed by the proxy`);
      const centre = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, tilePx);
      const centreTile = {
        x: Math.floor(centre.x / tilePx),
        y: Math.floor(centre.y / tilePx),
      };
      for (const tile of viewportTiles(zoom, tilePx, size, size)) {
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

test("smaller tiles never fan out past the per-layer cap", () => {
  // viewportTiles TRUNCATES at MAX_TILES_PER_LAYER, so hitting the cap
  // would silently leave holes in the map rather than raise anything.
  for (const size of MAP_SIZES) {
    const tilePx = tilePixelSize(size);
    for (const zoom of GROUNDWATER_ZOOMS) {
      const count = viewportTiles(zoom, tilePx, size, size).length;
      assert.ok(count < MAX_TILES_PER_LAYER, `${count} tiles at zoom ${zoom}, size ${size}`);
    }
  }
});

// --- framing: the stations reach out to the edge (Etappe 48) ---------------

test("the tile size is derived so the default zoom covers the target ground", () => {
  for (const size of MAP_SIZES) {
    const tilePx = tilePixelSize(size);
    // A whole-pixel tile edge (no hairline seams, see tilePixelSize) costs
    // at most half a tile pixel of ground — nothing else may.
    const slack = TARGET_COVERAGE_M / tilePx;
    assert.ok(Number.isInteger(tilePx), `tile edge ${tilePx} is not whole at size ${size}`);
    const covered = metresPerPixel(DEFAULT_GROUNDWATER_ZOOM, tilePx) * size;
    assert.ok(Math.abs(covered - TARGET_COVERAGE_M) < slack, `${covered} m at size ${size}`);
  }
  // Nothing typed: the target is the 100 km circle plus the margin.
  assert.ok(
    Math.abs(TARGET_COVERAGE_M - (2 * STATION_RADIUS_KM * 1000) / STATION_CIRCLE_FILL) < 1e-6,
  );
});

test("the kiosk map downscales its tiles rather than blowing them up", () => {
  // Fetching zoom 10 and drawing it SMALLER is what keeps the map crisp at
  // a scale no whole tile zoom offers.
  assert.ok(tilePixelSize(840) < NATIVE_TILE_PX, tilePixelSize(840));
  assert.ok(tilePixelSize(900) < NATIVE_TILE_PX, tilePixelSize(900));
});

test("one zoom step still halves the ground on screen, whatever the map size", () => {
  // This is what lets the density rule speak in zoom steps (see
  // SPARKLINE_MIN_ZOOM) instead of in metres per pixel.
  for (const size of MAP_SIZES) {
    const tilePx = tilePixelSize(size);
    for (let i = 1; i < GROUNDWATER_ZOOMS.length; i += 1) {
      const wide = metresPerPixel(GROUNDWATER_ZOOMS[i - 1], tilePx);
      const close = metresPerPixel(GROUNDWATER_ZOOMS[i], tilePx);
      assert.ok(Math.abs(wide / close - 2) < 1e-9, [size, wide, close]);
    }
  }
});

test("a nonsense map width still yields a drawable tile size", () => {
  for (const bad of [0, -100, Number.NaN, undefined, null]) {
    const tilePx = tilePixelSize(bad);
    assert.ok(Number.isFinite(tilePx) && tilePx > 0, String(bad));
  }
});

test("the default zoom shows the whole 50 km radius with a narrow margin", () => {
  // Otherwise stations at the edge of the list would simply be invisible
  // until Roland zooms out, without anything telling him they exist — but
  // the point of Etappe 48 is that they must not huddle in the middle
  // either: "die äussersten messpunkte können nah am rand sein".
  const dLat = RADIUS_KM / 111.0;
  const dLon = RADIUS_KM / (111.0 * Math.cos((MUNICH_LAT * Math.PI) / 180));
  const ring = [
    [MUNICH_LAT + dLat, MUNICH_LON],
    [MUNICH_LAT - dLat, MUNICH_LON],
    [MUNICH_LAT, MUNICH_LON + dLon],
    [MUNICH_LAT, MUNICH_LON - dLon],
  ];
  for (const size of MAP_SIZES) {
    const tilePx = tilePixelSize(size);
    for (const [lat, lon] of ring) {
      const point = markerPixel(lon, lat, DEFAULT_GROUNDWATER_ZOOM, tilePx, size, size);
      const margin = Math.min(point.x, size - point.x, point.y, size - point.y) / size;
      assert.ok(margin > 0.02, `50 km ring only ${(margin * 100).toFixed(1)}% inside at ${size}`);
      assert.ok(margin < 0.08, `50 km ring ${(margin * 100).toFixed(1)}% from the edge at ${size}`);
    }
  }
});

test("the real station bounding box sits close to the edge, not in the middle", () => {
  const size = 840; // the kiosk map
  const tilePx = tilePixelSize(size);
  const corners = [
    [STATION_BBOX.latMax, STATION_BBOX.lonMin],
    [STATION_BBOX.latMax, STATION_BBOX.lonMax],
    [STATION_BBOX.latMin, STATION_BBOX.lonMin],
    [STATION_BBOX.latMin, STATION_BBOX.lonMax],
  ];
  let smallest = 1;
  for (const [lat, lon] of corners) {
    const point = markerPixel(lon, lat, DEFAULT_GROUNDWATER_ZOOM, tilePx, size, size);
    assert.ok(point.x >= 0 && point.x <= size, `x ${point.x} outside the map`);
    assert.ok(point.y >= 0 && point.y <= size, `y ${point.y} outside the map`);
    const margin = Math.min(point.x, size - point.x, point.y, size - point.y) / size;
    smallest = Math.min(smallest, margin);
  }
  // Was ~21% before Etappe 48 — the stations sat in the middle of a mostly
  // empty map. A dot must still be drawn whole, hence the lower bound.
  assert.ok(smallest > 0.03, `outermost station only ${(smallest * 100).toFixed(1)}% inside`);
  assert.ok(smallest < 0.07, `outermost station still ${(smallest * 100).toFixed(1)}% from the edge`);
});

// --- density rule: dots only until the map is zoomed in (Etappe 47) --------

test("the sparkline threshold is one step closer than the overview default", () => {
  // Derived, not typed: the overview zoom is the one that shows the whole
  // radius, and the cards are meant to appear one step INTO it.
  assert.equal(SPARKLINE_MIN_ZOOM, DEFAULT_GROUNDWATER_ZOOM + 1);
  assert.ok(
    GROUNDWATER_ZOOMS.includes(SPARKLINE_MIN_ZOOM),
    "the threshold must be a zoom the buttons can actually reach",
  );
});

test("sparklines are off below the threshold and on from it", () => {
  assert.equal(showsSparklines(SPARKLINE_MIN_ZOOM - 1), false);
  // The boundary itself counts as "zoomed in".
  assert.equal(showsSparklines(SPARKLINE_MIN_ZOOM), true);
  assert.equal(showsSparklines(SPARKLINE_MIN_ZOOM + 1), true);
  assert.equal(showsSparklines(DEFAULT_GROUNDWATER_ZOOM), false);
  for (const zoom of GROUNDWATER_ZOOMS) {
    assert.equal(showsSparklines(zoom), zoom >= SPARKLINE_MIN_ZOOM, `zoom ${zoom}`);
  }
});

test("a nonsense zoom falls back to the plain dots", () => {
  // Fewer pixels of nothing is the safe side: a dot is always readable.
  for (const zoom of [undefined, null, Number.NaN, "10", {}]) {
    assert.equal(showsSparklines(zoom), false, String(zoom));
  }
});

test("markers below the threshold carry no card", () => {
  const [marker] = markers([station("1", MUNICH_LAT, MUNICH_LON)], DEFAULT_GROUNDWATER_ZOOM);
  assert.equal(marker.showsCard, false);
});

test("markers at the threshold carry a card", () => {
  const [marker] = markers([station("1", MUNICH_LAT, MUNICH_LON)], SPARKLINE_MIN_ZOOM);
  assert.equal(marker.showsCard, true);
});

test("the dot-only hit area is a touch-sized square on the dot", () => {
  const [marker] = markers([station("1", MUNICH_LAT, MUNICH_LON)], DEFAULT_GROUNDWATER_ZOOM);
  assert.ok(marker.hit.width >= 44 && marker.hit.height >= 44, marker.hit);
  assert.equal(marker.hit.x + marker.hit.width / 2, marker.x);
  assert.equal(marker.hit.y + marker.hit.height / 2, marker.y);
});

test("the card hit area covers card and dot together", () => {
  const [marker] = markers([station("1", MUNICH_LAT, MUNICH_LON)], SPARKLINE_MIN_ZOOM);
  assert.equal(marker.hit.x, marker.cardX);
  assert.equal(marker.hit.y, marker.cardY);
  assert.equal(marker.hit.width, CARD_WIDTH);
  assert.equal(marker.hit.y + marker.hit.height, marker.y + DOT_RADIUS);
});

test("stations sharing a well head stay separately tappable in both states", () => {
  for (const zoom of [DEFAULT_GROUNDWATER_ZOOM, SPARKLINE_MIN_ZOOM]) {
    const both = markers(
      [station("T3T", 48.171, 11.453), station("T3F", 48.171, 11.453)],
      zoom,
    );
    assert.equal(both.length, 2, `both stations must be drawn at zoom ${zoom}`);
    const byNumber = Object.fromEntries(both.map((marker) => [marker.number, marker]));
    // Same dot, stacked hit areas — neither finger target covers the other.
    assert.equal(byNumber.T3F.x, byNumber.T3T.x);
    assert.equal(byNumber.T3F.y, byNumber.T3T.y);
    assert.ok(
      byNumber.T3T.hit.y + byNumber.T3T.hit.height <= byNumber.T3F.hit.y,
      `overlapping hit areas at zoom ${zoom}`,
    );
    assert.deepEqual(byNumber.T3F.siblings, ["T3F", "T3T"]);
  }
});

test("the overview dot is bigger than the one under a card", () => {
  // Without the curve the colour of the dot is the only thing left, so it
  // has to carry from two metres away.
  assert.ok(OVERVIEW_DOT_RADIUS > DOT_RADIUS, [OVERVIEW_DOT_RADIUS, DOT_RADIUS]);
});
