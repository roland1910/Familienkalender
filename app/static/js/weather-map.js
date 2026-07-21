// Pure slippy-map math for the rain radar (no DOM), unit-testable with
// plain node --test. Mirrors the window the backend proxy accepts
// (app/weather.py: ALLOWED_ZOOMS / is_allowed_tile) — everything this
// module asks for must stay inside it.
//
// Why a pixel-centred viewport instead of a plain grid of whole tiles:
// Munich sits at a fractional position inside its own tile, so a grid of
// whole tiles would put it up to half a tile (≈100 km) off centre. The
// tiles are therefore positioned by pixel offset, exactly like a real
// slippy map, and the viewport is cropped by the container.
//
// Why two zoom levels per view: RainViewer's free radar tiles only exist
// up to zoom 7 — deeper zooms return a "Zoom Level Not Supported" image.
// The radar layer therefore stays at zoom <= 7 and its tiles are drawn at
// double size, while the base map is fetched one zoom level deeper at
// normal size. Both cover exactly the same ground, but the map stays
// crisp instead of being upscaled along with the radar.

export const MUNICH_LAT = 48.1374;
export const MUNICH_LON = 11.5755;

// Radar zoom levels behind the -/+ buttons (wide → close) and the default.
// 7 is RainViewer's maximum; drawn at RADAR_TILE_PX it shows Munich plus
// roughly 100 km of surroundings.
export const RADAR_ZOOMS = [5, 6, 7];
export const DEFAULT_ZOOM = 7;

// Rendered tile edge in CSS pixels. The radar is drawn at double size (see
// above); the base map is fetched one level deeper and drawn 1:1.
export const RADAR_TILE_PX = 512;
export const BASE_TILE_PX = 256;
export const BASE_ZOOM_OFFSET = 1;

// Hard cap on the tiles one layer may request, so an absurd container size
// can never fan out into hundreds of proxied requests.
export const MAX_TILES_PER_LAYER = 40;

/** The base map zoom that matches a radar zoom (same ground, sharper). */
export function baseZoomFor(radarZoom) {
  return radarZoom + BASE_ZOOM_OFFSET;
}

/**
 * World pixel coordinates of a lon/lat at `zoom`, for `tilePx`-sized
 * tiles (Web Mercator). Fractional on purpose — that is what keeps
 * Munich exactly in the middle of the viewport.
 */
export function projectPixel(lon, lat, zoom, tilePx) {
  const worldSize = 2 ** zoom * tilePx;
  const rad = (lat * Math.PI) / 180;
  return {
    x: ((lon + 180) / 360) * worldSize,
    y: ((1 - Math.asinh(Math.tan(rad)) / Math.PI) / 2) * worldSize,
  };
}

/**
 * The tiles covering a `width` x `height` viewport centred on Munich,
 * each with the CSS offset it must be placed at inside that viewport.
 * Tiles outside the world (at low zoom the viewport can be wider than the
 * map) are skipped; the result is capped at MAX_TILES_PER_LAYER.
 */
export function viewportTiles(zoom, tilePx, width, height) {
  const center = projectPixel(MUNICH_LON, MUNICH_LAT, zoom, tilePx);
  // World pixel of the viewport's top-left corner.
  const originX = center.x - width / 2;
  const originY = center.y - height / 2;
  const firstX = Math.floor(originX / tilePx);
  const firstY = Math.floor(originY / tilePx);
  const lastX = Math.floor((originX + width) / tilePx);
  const lastY = Math.floor((originY + height) / tilePx);
  const worldTiles = 2 ** zoom;
  const tiles = [];
  for (let y = firstY; y <= lastY; y += 1) {
    for (let x = firstX; x <= lastX; x += 1) {
      if (x < 0 || y < 0 || x >= worldTiles || y >= worldTiles) continue;
      tiles.push({
        x,
        y,
        zoom,
        left: x * tilePx - originX,
        top: y * tilePx - originY,
        size: tilePx,
      });
      if (tiles.length >= MAX_TILES_PER_LAYER) return tiles;
    }
  }
  return tiles;
}

/**
 * Step the zoom by `delta` levels within RADAR_ZOOMS, clamped at both
 * ends so the buttons stay harmless at the limits.
 */
export function stepZoom(zoom, delta) {
  const index = RADAR_ZOOMS.indexOf(zoom);
  const from = index === -1 ? RADAR_ZOOMS.indexOf(DEFAULT_ZOOM) : index;
  const next = Math.min(RADAR_ZOOMS.length - 1, Math.max(0, from + delta));
  return RADAR_ZOOMS[next];
}
