// Unit tests for the radar tile grid math. The grid must stay centred on
// Munich and inside the window the backend proxy accepts (app/weather.py:
// ALLOWED_ZOOMS and MAX_TILE_RADIUS = 3 around Munich's own tile).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  centerTile,
  DEFAULT_ZOOM,
  GRID_COLS,
  GRID_ROWS,
  latToTileY,
  lonToTileX,
  stepZoom,
  tileGrid,
  ZOOM_LEVELS,
} from "../../app/static/js/weather-map.js";

// Backend-side limit; duplicated here on purpose so a change on either
// side makes this test fail rather than the kiosk showing empty tiles.
const BACKEND_TILE_RADIUS = 3;

test("centerTile matches the standard slippy-map tile numbers for Munich", () => {
  assert.deepEqual(centerTile(8), { x: 136, y: 88 });
  assert.deepEqual(centerTile(9), { x: 272, y: 177 });
  assert.deepEqual(centerTile(10), { x: 544, y: 355 });
});

test("lonToTileX and latToTileY stay inside the world at every zoom", () => {
  for (const zoom of ZOOM_LEVELS) {
    const max = 2 ** zoom;
    assert.ok(lonToTileX(-180, zoom) >= 0);
    assert.ok(lonToTileX(179.9, zoom) < max);
    assert.ok(latToTileY(85, zoom) >= 0);
    assert.ok(latToTileY(-85, zoom) < max);
  }
});

test("tileGrid has one entry per cell, in row-major order", () => {
  const tiles = tileGrid(DEFAULT_ZOOM);
  assert.equal(tiles.length, GRID_COLS * GRID_ROWS);
  assert.deepEqual(
    tiles.map((tile) => `${tile.row}/${tile.col}`).slice(0, GRID_COLS + 1),
    ["0/0", "0/1", "0/2", "0/3", "0/4", "1/0"],
  );
});

test("tileGrid is centred on Munich's own tile", () => {
  const center = centerTile(DEFAULT_ZOOM);
  const tiles = tileGrid(DEFAULT_ZOOM);
  const middle = tiles[Math.floor(tiles.length / 2)];
  assert.equal(middle.x, center.x);
  assert.equal(middle.y, center.y);
});

test("every requested tile is inside the window the backend proxies", () => {
  for (const zoom of ZOOM_LEVELS) {
    const center = centerTile(zoom);
    for (const tile of tileGrid(zoom)) {
      assert.ok(
        Math.abs(tile.x - center.x) <= BACKEND_TILE_RADIUS,
        `x out of range at zoom ${zoom}`,
      );
      assert.ok(
        Math.abs(tile.y - center.y) <= BACKEND_TILE_RADIUS,
        `y out of range at zoom ${zoom}`,
      );
    }
  }
});

test("stepZoom moves through the levels and clamps at both ends", () => {
  assert.equal(stepZoom(9, 1), 10);
  assert.equal(stepZoom(9, -1), 8);
  assert.equal(stepZoom(10, 1), 10);
  assert.equal(stepZoom(8, -1), 8);
});

test("stepZoom recovers from an unknown zoom via the default", () => {
  assert.equal(stepZoom(3, 0), DEFAULT_ZOOM);
  assert.equal(stepZoom(null, 1), 10);
});
