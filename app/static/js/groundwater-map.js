// Pure placement math for the groundwater station markers (no DOM),
// unit-testable with plain node --test.
//
// The map itself is the rain radar's base map: same proxied OSM tiles, same
// `viewportTiles` (see weather-map.js), no radar layer. Only the scale
// differs — the radar shows a weather system, this view shows ~178 stations
// inside a 50 km radius around Munich and has to frame exactly that circle,
// which no whole tile zoom does (see GROUNDWATER_ZOOMS / tilePixelSize).
//
// Markers are placed in PIXELS against the very same viewport origin the
// tiles use (`viewportOrigin`). Anything else — a per-tile grid, a fresh
// projection — would drift as soon as the zoom changes, and a station dot
// that is 3 km off its well is worse than no dot.

import { MUNICH_LAT, MUNICH_LON, projectPixel, stepWithin, viewportOrigin } from "./weather-map.js";

// Zoom levels behind the -/+ buttons (wide → close) and the default.
//
// No WHOLE tile zoom frames this view well: the stations live inside a
// 50 km radius, so the map has to cover ~100 km, and on a ~840 px kiosk map
// that is ~119 m/px — between zoom 9 (204 m/px, stations clinging to the
// middle with a third of the map empty around them) and zoom 10 (102 m/px,
// the outermost stations falling off every side). The way out is the one
// the rain radar already uses: draw tiles at a size other than their native
// 256 px. Here the direction is the better one — zoom 10 tiles drawn
// SMALLER, i.e. downscaled, which stays crisp (see tilePixelSize).
export const GROUNDWATER_ZOOMS = [9, 10, 11];
export const DEFAULT_GROUNDWATER_ZOOM = 10;

// --- how much ground the default view shows --------------------------------

// Radius of the station list, mirrored from RADIUS_KM in app/groundwater.py.
// Duplicated on purpose: a one-sided change must show up in the tests here
// rather than as stations sitting outside the map on the kiosk.
export const STATION_RADIUS_KM = 50;

// Share of the map edge the stations' 100 km circle is meant to fill. The
// remaining 8% are the margin, 4% per side — enough that a dot at the very
// rim is drawn whole instead of being cut by the edge, and little enough
// that the stations reach out to the border instead of huddling in the
// middle ("die äussersten messpunkte können nah am rand sein").
export const STATION_CIRCLE_FILL = 0.92;

/** Ground distance the default view spans, in metres (~109 km). */
export const TARGET_COVERAGE_M = (2 * STATION_RADIUS_KM * 1000) / STATION_CIRCLE_FILL;

// Native edge of an OSM tile. Everything the proxy serves is 256 px; the
// rendered size is a separate matter (see tilePixelSize).
export const NATIVE_TILE_PX = 256;

const EARTH_CIRCUMFERENCE_M = 40075016.686;

/** Ground metres one rendered pixel covers at Munich's latitude. */
export function metresPerPixel(zoom, tilePx) {
  const munichCircumference = EARTH_CIRCUMFERENCE_M * Math.cos((MUNICH_LAT * Math.PI) / 180);
  return munichCircumference / (2 ** zoom * tilePx);
}

/**
 * The edge, in CSS pixels, at which a tile is DRAWN on a `mapPx` wide map,
 * so that the default zoom covers exactly TARGET_COVERAGE_M.
 *
 * Derived rather than typed, because the map size comes from the auto-fit
 * and is not 840 px everywhere. Two consequences worth knowing:
 *
 *   - the tile size scales with the map, so the view always frames the same
 *     ground and always asks for the same NUMBER of tiles (~5x6, checked in
 *     tests/js/groundwater-map.test.mjs against the proxy's window);
 *   - one zoom step therefore still halves (or doubles) the ground on
 *     screen at any map size — which is what keeps the density rule below
 *     honest, see SPARKLINE_MIN_ZOOM.
 *
 * On the kiosk this comes out at 202 px, i.e. a 256 px tile drawn smaller.
 * Downscaling keeps the map sharp; only a map wider than ~1000 px would
 * start upscaling, and MAX_MAP_PX caps how far that can go.
 *
 * Rounded to a WHOLE pixel on purpose. Neighbouring tiles are laid out at
 * `x * tilePx - originX`, so with an integer edge they all share the same
 * fractional offset and the browser rounds them the same way; a fractional
 * edge would let two neighbours round apart and leave hairline seams across
 * the map. The price is under a pixel of coverage, which the tests allow for.
 */
export function tilePixelSize(mapPx) {
  // A viewport that has not been laid out yet must not produce a zero or
  // negative tile size — fall back to the native edge, which is drawable.
  const width = Number.isFinite(mapPx) && mapPx > 0 ? mapPx : NATIVE_TILE_PX;
  const wanted = TARGET_COVERAGE_M / width; // metres per rendered pixel
  const exact =
    (metresPerPixel(DEFAULT_GROUNDWATER_ZOOM, NATIVE_TILE_PX) * NATIVE_TILE_PX) / wanted;
  return Math.max(1, Math.round(exact));
}

// DENSITY RULE (Etappe 47, from a photo of the real kiosk): below this zoom
// only the coloured dots are drawn, the sparkline cards appear from here on.
//
// At the overview zoom the whole 50 km radius is on screen, and out at the
// rim the cards read beautifully — but around Munich the stations stand so
// close that their 44x22 cards overlap into a block in which no card can be
// matched to a dot any more. That is the one part of the map that matters
// most, and from two metres it was unreadable.
//
// So the overview is a clean traffic-light map ("where is it critical") and
// the curves appear once a corner of it is actually being looked at — the
// order in which such a map is really read. The threshold is DERIVED from
// the overview zoom rather than typed: the cards belong one step INTO the
// map, whatever the default happens to be.
//
// Stated over the zoom INDEX although the rendered tile size now varies —
// on purpose. The tile size scales with the map (tilePixelSize), so one
// zoom step halves the ground on screen on every display: the index and the
// effective resolution say the same thing, and the index says it in one
// comparison. What matters for card overlap is how much ground shares the
// map, and that is exactly what a step changes (~109 km across at the
// default, ~54 km one step in).
export const SPARKLINE_MIN_ZOOM = DEFAULT_GROUNDWATER_ZOOM + 1;

/** True when the map is close enough to draw the sparkline cards. */
export function showsSparklines(zoom) {
  // Anything unusable errs towards the dots: a dot is readable at every
  // scale, a pile of cards is not.
  return typeof zoom === "number" && Number.isFinite(zoom) && zoom >= SPARKLINE_MIN_ZOOM;
}

// Hard cap on the rendered map edge. With a derived tile size a huge map no
// longer asks for MORE tiles (it asks for bigger ones), so the cap is now
// about sharpness and sanity: beyond ~1000 px the 256 px tiles are being
// upscaled, and a 4K monitor would blow them up without showing more.
// Checked in tests/js/groundwater-map.test.mjs against the backend's window.
export const MAX_MAP_PX = 1200;

// Marker geometry, in CSS pixels. The card holds the sparkline and sits
// above the dot, which marks the true position.
export const DOT_RADIUS = 4;
export const CARD_WIDTH = 44;
export const CARD_HEIGHT = 22;
// Gap between the dot and the card stack, and between two stacked cards.
export const CARD_GAP = 6;
export const STACK_GAP = 3;

// Dot radius in the overview, where no card is drawn. Below the threshold
// the colour of the dot is the ONLY thing the marker still says, so it has
// to carry from the ~2 m the kiosk is read at; the 4 px dot was sized to sit
// under a card, not to be read on its own. 7 px matches the legend swatch,
// which is exactly the comparison the eye makes here.
export const OVERVIEW_DOT_RADIUS = 7;

// Edge of the invisible square that takes the tap in the overview. The
// visible dot may be small, the finger target may not (the project's 44 px
// rule). Siblings at one well head get theirs stacked, same idea as the
// cards — two coincident targets would make one of them unreachable.
export const DOT_HIT_SIZE = 44;

// How far outside the viewport a marker may sit and still be built. Beyond
// that it is dropped: at the default zoom all of the ~178 stations are on
// screen, but one step in three quarters are not, and building their DOM
// would be waste.
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
 * Returns `[{number, x, y, cardX, cardY, showsCard, hit, stackIndex, siblings}]`:
 *   - `x`/`y` is the true position of the well (where the dot goes),
 *   - `cardX`/`cardY` is the top-left of the sparkline card. It is computed
 *     at every zoom, so a caller can place a card without asking twice —
 *     whether one is DRAWN is `showsCard`,
 *   - `showsCard` follows the density rule (see SPARKLINE_MIN_ZOOM),
 *   - `hit` is the rectangle that takes the tap: the card plus the dot when
 *     a card is drawn, a DOT_HIT_SIZE square on the dot when it is not,
 *   - `stackIndex` is 0 for the first station at a position; further ones
 *     get their card (and their hit area) stacked ABOVE it instead of being
 *     shifted sideways, so both stay tappable while neither dot lies about
 *     its longitude,
 *   - `siblings` lists the other station numbers at the same coordinates,
 *     which the detail popover offers as a switch (a mis-tap on a 22 px
 *     card row must be recoverable).
 *
 * Painted back to front by latitude: southern markers (larger y) come last
 * and therefore end up on top, the usual map convention.
 */
/**
 * The tap rectangle of one marker — the only place that knows the two
 * states apart geometrically, so the view can stay free of the decision.
 */
function hitArea(x, y, cardX, cardY, stackIndex, showsCard) {
  if (!showsCard) {
    // A square centred on the dot; a sibling's square sits directly above
    // it (the dots coincide, the targets must not).
    return {
      x: x - DOT_HIT_SIZE / 2,
      y: y - DOT_HIT_SIZE / 2 - stackIndex * DOT_HIT_SIZE,
      width: DOT_HIT_SIZE,
      height: DOT_HIT_SIZE,
    };
  }
  // With a card the target is the card itself, reaching down to the dot —
  // but only for the lowest of a stack. A stacked card must NOT reach down,
  // or it would cover the card below it and make that station unclickable,
  // which is exactly what the pair must never be.
  const bottom = stackIndex === 0 ? y + DOT_RADIUS : cardY + CARD_HEIGHT;
  return { x: cardX, y: cardY, width: CARD_WIDTH, height: bottom - cardY };
}

export function stationMarkers(stations, zoom, tilePx, width, height) {
  const showsCard = showsSparklines(zoom);
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
      const cardX = x - CARD_WIDTH / 2;
      const cardY =
        y - DOT_RADIUS - CARD_GAP - (stackIndex + 1) * CARD_HEIGHT - stackIndex * STACK_GAP;
      markers.push({
        number: station.number,
        x,
        y,
        cardX,
        cardY,
        showsCard,
        hit: hitArea(x, y, cardX, cardY, stackIndex, showsCard),
        stackIndex,
        siblings: siblings.length > 1 ? siblings : [],
      });
    });
  }
  markers.sort((a, b) => a.y - b.y || a.number.localeCompare(b.number));
  return markers;
}
