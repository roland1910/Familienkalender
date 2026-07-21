// Pure slippy-map tile math for the rain radar (no DOM), unit-testable
// with plain node --test. Mirrors the same formula the backend uses to
// decide which tiles it will proxy (app/weather.py munich_tile /
// is_allowed_tile) — the grid built here must stay inside that window.

export const TILE_SIZE = 256;
// The visible tile grid. Odd counts so the centre tile is Munich's own
// tile; 5x3 gives a wide band that matches the card's shape.
export const GRID_COLS = 5;
export const GRID_ROWS = 3;

// Zoom levels offered by the +/- buttons (wide → close) and the default:
// Munich plus its surroundings, roughly 260 km across at zoom 9.
export const ZOOM_LEVELS = [8, 9, 10];
export const DEFAULT_ZOOM = 9;

export const MUNICH_LAT = 48.1374;
export const MUNICH_LON = 11.5755;

/** Tile column containing `lon` at `zoom`. */
export function lonToTileX(lon, zoom) {
  return Math.floor(((lon + 180) / 360) * 2 ** zoom);
}

/** Tile row containing `lat` at `zoom` (Web Mercator). */
export function latToTileY(lat, zoom) {
  const rad = (lat * Math.PI) / 180;
  return Math.floor(((1 - Math.asinh(Math.tan(rad)) / Math.PI) / 2) * 2 ** zoom);
}

/** The tile Munich sits in, at `zoom`. */
export function centerTile(zoom) {
  return { x: lonToTileX(MUNICH_LON, zoom), y: latToTileY(MUNICH_LAT, zoom) };
}

/**
 * The visible tiles, centred on Munich, in row-major order (so they can
 * be appended straight into a CSS grid). Each entry carries the tile
 * coordinates plus its position in the grid.
 */
export function tileGrid(zoom, cols = GRID_COLS, rows = GRID_ROWS) {
  const center = centerTile(zoom);
  const startX = center.x - Math.floor(cols / 2);
  const startY = center.y - Math.floor(rows / 2);
  const tiles = [];
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      tiles.push({ x: startX + col, y: startY + row, col, row, zoom });
    }
  }
  return tiles;
}

/**
 * Step the zoom by `delta` steps within ZOOM_LEVELS, clamped at both
 * ends (the buttons stay harmless when already at the limit).
 */
export function stepZoom(zoom, delta) {
  const index = ZOOM_LEVELS.indexOf(zoom);
  const from = index === -1 ? ZOOM_LEVELS.indexOf(DEFAULT_ZOOM) : index;
  const next = Math.min(ZOOM_LEVELS.length - 1, Math.max(0, from + delta));
  return ZOOM_LEVELS[next];
}
