// Pure placement math for the groundwater station markers (no DOM),
// unit-testable with plain node --test.
//
// The map itself is the rain radar's base map: same proxied OSM tiles, same
// `viewportTiles` (see weather-map.js), no radar layer. Only the zoom levels
// differ — the radar shows a weather system, this view shows ~165 stations
// inside a 50 km radius around Munich and needs a much closer scale.
//
// Markers are placed in PIXELS against the very same viewport origin the
// tiles use (`viewportOrigin`). Anything else — a per-tile grid, a fresh
// projection — would drift as soon as the zoom changes, and a station dot
// that is 3 km off its well is worse than no dot.

import { MUNICH_LAT, MUNICH_LON, projectPixel, stepWithin, viewportOrigin } from "./weather-map.js";

// Zoom levels behind the -/+ buttons (wide → close) and the default.
// Metres per pixel at Munich's latitude: ~408 at zoom 8, ~204 at 9, ~102
// at 10. On a ~900 px kiosk map that is 367 km / 184 km / 92 km across —
// the 100 km circle of stations is a quarter of the map at zoom 8 and
// fills it at 9, which is why 9 is the default.
export const GROUNDWATER_ZOOMS = [8, 9, 10];
export const DEFAULT_GROUNDWATER_ZOOM = 9;

// The base map is drawn at its native tile size (no upscaling — unlike the
// radar, nothing forces a coarser layer underneath).
export const MAP_TILE_PX = 256;

// Hard cap on the rendered map edge. The backend proxy only serves tiles
// within MAX_TILE_RADIUS (4) of Munich's own tile; a map far larger than a
// kiosk screen (a 4K monitor, a zoomed-out browser) would ask for tiles
// outside that window and get 400s. 1200 px keeps the worst case at three
// tiles from the centre — checked in tests/js/groundwater-map.test.mjs
// against the duplicated backend limits.
export const MAX_MAP_PX = 1200;

// Marker geometry, in CSS pixels. The card holds the sparkline and sits
// above the dot, which marks the true position.
export const DOT_RADIUS = 4;
export const CARD_WIDTH = 44;
export const CARD_HEIGHT = 22;
// Gap between the dot and the card stack, and between two stacked cards.
export const CARD_GAP = 6;
export const STACK_GAP = 3;

// How far outside the viewport a marker may sit and still be built. Beyond
// that it is dropped: at zoom 8 most of the 165 stations are on screen, but
// at zoom 10 two thirds are not, and building their DOM would be waste.
const OFF_SCREEN_MARGIN = CARD_WIDTH;

/** Step the groundwater zoom within GROUNDWATER_ZOOMS (clamped at both ends). */
export function stepGroundwaterZoom(zoom, delta) {
  return stepWithin(GROUNDWATER_ZOOMS, zoom, delta, DEFAULT_GROUNDWATER_ZOOM);
}

/** The map edge actually rendered: square, never larger than MAX_MAP_PX. */
export function mapSize(available) {
  return Math.max(0, Math.min(MAX_MAP_PX, Math.floor(available)));
}

/**
 * Pixel position of a lon/lat inside a `width` x `height` viewport centred
 * on Munich — the same origin the tiles are laid out against.
 */
export function markerPixel(lon, lat, zoom, tilePx, width, height) {
  const origin = viewportOrigin(zoom, tilePx, width, height);
  const point = projectPixel(lon, lat, zoom, tilePx);
  return { x: point.x - origin.x, y: point.y - origin.y };
}

/** Munich's own pixel position — by definition the centre of the viewport. */
export function munichPixel(zoom, tilePx, width, height) {
  return markerPixel(MUNICH_LON, MUNICH_LAT, zoom, tilePx, width, height);
}

function isDrawable(station) {
  return (
    station !== null &&
    typeof station === "object" &&
    Number.isFinite(station.lat) &&
    Number.isFinite(station.lon) &&
    typeof station.number === "string" &&
    station.number !== ""
  );
}

// Stations at EXACTLY the same coordinates are a real case, not an edge one:
// Obermenzing T 3 F (upper aquifer) and T 3 T (deeper one) share a well head
// and differ by ~8 m in water level. Their key is the coordinate pair, and
// the identity within the group is the station NUMBER — never the position.
function positionKey(station) {
  return `${station.lat}|${station.lon}`;
}

/**
 * Screen markers for `stations` inside a `width` x `height` viewport.
 *
 * Returns `[{number, x, y, cardX, cardY, stackIndex, siblings}]`:
 *   - `x`/`y` is the true position of the well (where the dot goes),
 *   - `cardX`/`cardY` is the top-left of the sparkline card,
 *   - `stackIndex` is 0 for the first station at a position; further ones
 *     get their card stacked ABOVE it instead of being shifted sideways,
 *     so both stay tappable while neither dot lies about its longitude,
 *   - `siblings` lists the other station numbers at the same coordinates,
 *     which the detail popover offers as a switch (a mis-tap on a 22 px
 *     card row must be recoverable).
 *
 * Painted back to front by latitude: southern markers (larger y) come last
 * and therefore end up on top, the usual map convention.
 */
export function stationMarkers(stations, zoom, tilePx, width, height) {
  const drawable = (Array.isArray(stations) ? stations : []).filter(isDrawable);
  const groups = new Map();
  for (const station of drawable) {
    const key = positionKey(station);
    const group = groups.get(key);
    if (group === undefined) groups.set(key, [station]);
    else group.push(station);
  }
  const markers = [];
  for (const group of groups.values()) {
    // Deterministic order within a group, so a reload cannot shuffle the
    // stack and move a card out from under Roland's finger.
    const ordered = [...group].sort((a, b) => a.number.localeCompare(b.number));
    const siblings = ordered.map((station) => station.number);
    ordered.forEach((station, stackIndex) => {
      const { x, y } = markerPixel(station.lon, station.lat, zoom, tilePx, width, height);
      if (
        x < -OFF_SCREEN_MARGIN ||
        y < -OFF_SCREEN_MARGIN ||
        x > width + OFF_SCREEN_MARGIN ||
        y > height + OFF_SCREEN_MARGIN
      ) {
        return;
      }
      markers.push({
        number: station.number,
        x,
        y,
        cardX: x - CARD_WIDTH / 2,
        cardY: y - DOT_RADIUS - CARD_GAP - (stackIndex + 1) * CARD_HEIGHT - stackIndex * STACK_GAP,
        stackIndex,
        siblings: siblings.length > 1 ? siblings : [],
      });
    });
  }
  markers.sort((a, b) => a.y - b.y || a.number.localeCompare(b.number));
  return markers;
}
