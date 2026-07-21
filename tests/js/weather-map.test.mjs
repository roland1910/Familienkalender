// Unit tests for the radar viewport math. The tiles must keep Munich in
// the exact centre and stay inside the window the backend proxy accepts
// (app/weather.py: ALLOWED_ZOOMS and MAX_TILE_RADIUS around Munich).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BASE_TILE_PX,
  baseZoomFor,
  DEFAULT_ZOOM,
  MAX_TILES_PER_LAYER,
  MUNICH_LAT,
  MUNICH_LON,
  projectPixel,
  RADAR_TILE_PX,
  RADAR_ZOOMS,
  stepZoom,
  viewportTiles,
} from "../../app/static/js/weather-map.js";

// Backend-side limits, duplicated on purpose so a change on either side
// fails here instead of showing empty tiles on the kiosk.
const BACKEND_ZOOMS = [5, 6, 7, 8];
const BACKEND_TILE_RADIUS = 4;

// The largest viewport the layout can produce (see the CSS clamp).
const WIDE = { width: 1400, height: 520 };

function centerTile(zoom, tilePx) {
  const center = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, tilePx);
  return { x: Math.floor(center.x / tilePx), y: Math.floor(center.y / tilePx) };
}

test("projectPixel reproduces the standard slippy-map tile numbers", () => {
  assert.deepEqual(centerTile(8, 256), { x: 136, y: 88 });
  assert.deepEqual(centerTile(7, 256), { x: 68, y: 44 });
  assert.deepEqual(centerTile(9, 256), { x: 272, y: 177 });
});

test("projectPixel scales linearly with the tile size", () => {
  const small = projectPixel(MUNICH_LON, MUNICH_LAT, 7, 256);
  const large = projectPixel(MUNICH_LON, MUNICH_LAT, 7, 512);
  assert.ok(Math.abs(large.x - small.x * 2) < 1e-6);
  assert.ok(Math.abs(large.y - small.y * 2) < 1e-6);
});

test("the base map zoom shows the same ground as the radar zoom", () => {
  // One level deeper at half the tile size covers exactly the same area.
  for (const zoom of RADAR_ZOOMS) {
    const radar = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, RADAR_TILE_PX);
    const base = projectPixel(MUNICH_LON, MUNICH_LAT, baseZoomFor(zoom), BASE_TILE_PX);
    assert.ok(Math.abs(radar.x - base.x) < 1e-6, `x mismatch at zoom ${zoom}`);
    assert.ok(Math.abs(radar.y - base.y) < 1e-6, `y mismatch at zoom ${zoom}`);
  }
});

test("radar zoom levels never exceed RainViewer's maximum of 7", () => {
  // Deeper zooms answer with a "Zoom Level Not Supported" placeholder.
  assert.ok(Math.max(...RADAR_ZOOMS) <= 7);
  assert.ok(RADAR_ZOOMS.includes(DEFAULT_ZOOM));
});

test("viewportTiles covers the whole viewport with no gaps", () => {
  const tiles = viewportTiles(DEFAULT_ZOOM, RADAR_TILE_PX, WIDE.width, WIDE.height);
  assert.ok(tiles.length > 0);
  // Every edge of the viewport is covered by some tile.
  const covers = (px, py) =>
    tiles.some(
      (tile) =>
        tile.left <= px && px < tile.left + tile.size && tile.top <= py && py < tile.top + tile.size,
    );
  assert.ok(covers(0, 0), "top-left uncovered");
  assert.ok(covers(WIDE.width - 1, 0), "top-right uncovered");
  assert.ok(covers(0, WIDE.height - 1), "bottom-left uncovered");
  assert.ok(covers(WIDE.width - 1, WIDE.height - 1), "bottom-right uncovered");
  assert.ok(covers(WIDE.width / 2, WIDE.height / 2), "centre uncovered");
});

test("viewportTiles puts Munich exactly in the middle of the viewport", () => {
  for (const zoom of RADAR_ZOOMS) {
    const tiles = viewportTiles(zoom, RADAR_TILE_PX, WIDE.width, WIDE.height);
    const center = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, RADAR_TILE_PX);
    // The tile containing Munich, and where Munich lands inside the viewport.
    const host = tiles.find(
      (tile) =>
        tile.x === Math.floor(center.x / RADAR_TILE_PX) &&
        tile.y === Math.floor(center.y / RADAR_TILE_PX),
    );
    assert.ok(host !== undefined, `Munich's own tile missing at zoom ${zoom}`);
    const screenX = host.left + (center.x - host.x * RADAR_TILE_PX);
    const screenY = host.top + (center.y - host.y * RADAR_TILE_PX);
    assert.ok(Math.abs(screenX - WIDE.width / 2) < 1e-6, `x off centre at zoom ${zoom}`);
    assert.ok(Math.abs(screenY - WIDE.height / 2) < 1e-6, `y off centre at zoom ${zoom}`);
  }
});

test("every requested tile is inside the window the backend proxies", () => {
  const viewports = [WIDE, { width: 420, height: 260 }, { width: 1920, height: 520 }];
  for (const viewport of viewports) {
    for (const zoom of RADAR_ZOOMS) {
      const layers = [
        { zoom, tilePx: RADAR_TILE_PX },
        { zoom: baseZoomFor(zoom), tilePx: BASE_TILE_PX },
      ];
      for (const layer of layers) {
        assert.ok(BACKEND_ZOOMS.includes(layer.zoom), `zoom ${layer.zoom} not allowed`);
        const center = centerTile(layer.zoom, layer.tilePx);
        for (const tile of viewportTiles(
          layer.zoom,
          layer.tilePx,
          viewport.width,
          viewport.height,
        )) {
          assert.ok(
            Math.abs(tile.x - center.x) <= BACKEND_TILE_RADIUS,
            `x out of range at zoom ${layer.zoom}`,
          );
          assert.ok(
            Math.abs(tile.y - center.y) <= BACKEND_TILE_RADIUS,
            `y out of range at zoom ${layer.zoom}`,
          );
        }
      }
    }
  }
});

test("viewportTiles never returns more tiles than the cap", () => {
  const tiles = viewportTiles(7, 256, 100000, 100000);
  assert.equal(tiles.length, MAX_TILES_PER_LAYER);
});

test("viewportTiles skips tiles outside the world", () => {
  // At zoom 5 a very wide viewport reaches past the map edge; no tile may
  // have a negative or out-of-range index (the proxy would reject those).
  for (const tile of viewportTiles(5, 512, 4000, 2000)) {
    assert.ok(tile.x >= 0 && tile.x < 2 ** 5);
    assert.ok(tile.y >= 0 && tile.y < 2 ** 5);
  }
});

test("stepZoom moves through the levels and clamps at both ends", () => {
  assert.equal(stepZoom(6, 1), 7);
  assert.equal(stepZoom(6, -1), 5);
  assert.equal(stepZoom(7, 1), 7);
  assert.equal(stepZoom(5, -1), 5);
});

test("stepZoom recovers from an unknown zoom via the default", () => {
  assert.equal(stepZoom(99, 0), DEFAULT_ZOOM);
  assert.equal(stepZoom(null, -1), 6);
});
