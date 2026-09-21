// Groundwater view: the GKD Bayern monitoring stations around Munich as
// markers on the proxied OSM base map, each carrying a tiny history curve;
// tapping one opens its detail popover with the full curve.
//
// The weather view is the blueprint throughout: the same tile proxy and the
// same pixel-placed viewport (weather-map.js), the same auto-fit idea, the
// same timer ownership — every timer and listener started here is torn down
// by stopGroundwaterView(). Every dynamic string is rendered via
// textContent, and all SVG is built with createElementNS — never HTML.
//
// A note that saves the next reader a puzzled minute: EVERY MARKER
// SPARKLINE HAS ITS OWN Y SCALE. The absolute water tables inside the 50 km
// radius run from ~404 m to ~660 m above sea level while a single station
// moves by perhaps half a metre a year, so one shared scale would draw all
// ~165 curves as flat lines at different heights. The reasoning in full is
// in groundwater-chart.js.

import {
  baseTileUrl,
  fetchGroundwaterHistory,
  fetchGroundwaterSparklines,
  fetchGroundwaterStations,
} from "./api.js";
import { el } from "./dom.js";
import {
  detailArea,
  levelAxis,
  levelPath,
  levelTicks,
  scaleTime,
  sparklinePolyline,
  timeBounds,
} from "./groundwater-chart.js";
import {
  formatAxisDay,
  formatAxisLevel,
  formatDepth,
  formatLevel,
  formatMeasuredAt,
  legendEntries,
  situationColorVar,
  situationLabel,
  stationCountLabel,
  tierLabel,
} from "./groundwater-format.js";
import {
  CARD_HEIGHT,
  CARD_WIDTH,
  DEFAULT_GROUNDWATER_ZOOM,
  DOT_RADIUS,
  MAP_TILE_PX,
  mapSize,
  OVERVIEW_DOT_RADIUS,
  stationMarkers,
  stepGroundwaterZoom,
} from "./groundwater-map.js";
import { viewportTiles } from "./weather-map.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// The GKD publishes new readings roughly daily and the backend refetches
// once a day; half an hour is a polite cadence for a kiosk that may sit on
// this view all afternoon.
const REFRESH_MS = 30 * 60 * 1000;
// Debounce before rebuilding the pixel-placed tiles for a new window size.
const RESIZE_DEBOUNCE_MS = 300;

// Viewport size assumed while the map element is not laid out yet
// (measuring a display:none section returns 0).
const FALLBACK_MAP_PX = 600;
// Floor so the map never collapses to an unreadable sliver on a short kiosk.
const MAP_MIN_PX = 240;
// Below this width the side-by-side split stacks and the view may scroll;
// the map then takes its size from the CSS aspect-ratio (same breakpoint as
// the weather view's auto-fit).
const AUTOFIT_MIN_WIDTH = 901;

// Sparkline windows offered under the map. Must match SPARKLINE_ALLOWED_DAYS
// in app/groundwater.py; anything else is clamped there to the default.
const PERIODS = [
  { days: 30, label: "30 Tage" },
  { days: 90, label: "90 Tage" },
  { days: 365, label: "1 Jahr" },
];
const DEFAULT_DAYS = 90;

// Detail chart geometry (CSS pixels — the viewBox is the host's real size).
const DETAIL_PADDING = { top: 14, right: 16, bottom: 30, left: 62 };
const DETAIL_FALLBACK = { width: 620, height: 240 };
const DETAIL_TEXT_PX = 12;

// A new record low (class 3) gets a bigger dot on top of its colour:
// classes 2 and 3 are both red, and colour alone is not a distinction.
// Added to whichever base radius the current zoom uses, so the distinction
// survives into the dots-only overview.
const RECORD_LOW_CLASS = 3;
const RECORD_LOW_DOT_BONUS = 2;

// The licence of the data (CC BY 4.0) requires naming the source; this is
// not optional decoration. The NID rating and the base map carry their own
// obligations.
const ATTRIBUTION =
  "Datenquelle: Bayerisches Landesamt für Umwelt, www.lfu.bayern.de · " +
  "Niedrigwasser-Einstufung: Niedrigwasser-Informationsdienst Bayern · " +
  "Karte: © OpenStreetMap-Mitwirkende";

let activeContainer = null;
let refreshTimer = null;
let resizeTimer = null;
let zoomLevel = DEFAULT_GROUNDWATER_ZOOM;
let selectedDays = DEFAULT_DAYS;
let stations = [];
let sparklines = {};
// Bumped on every restart and reload so a late response from a previous
// state (view switched, period changed) is dropped instead of applied.
let generation = 0;
let openStationNumber = null;

/** Start loading and rendering into `container` (idempotent). */
export function startGroundwaterView(container) {
  stopGroundwaterView();
  activeContainer = container;
  zoomLevel = DEFAULT_GROUNDWATER_ZOOM;
  selectedDays = DEFAULT_DAYS;
  stations = [];
  sparklines = {};
  generation += 1;
  container.replaceChildren(buildSkeleton());
  loadAll();
  refreshTimer = setInterval(loadAll, REFRESH_MS);
  window.addEventListener("resize", onWindowResized);
  document.addEventListener("keydown", onKeyDown);
  // Registered here rather than per open, so repeated taps cannot stack up
  // listeners on the shared backdrop element.
  popoverNode()?.addEventListener("click", onBackdropClick);
}

/** Stop polling and drop every listener (e.g. when leaving the view). */
export function stopGroundwaterView() {
  if (refreshTimer !== null) clearInterval(refreshTimer);
  refreshTimer = null;
  clearTimeout(resizeTimer);
  resizeTimer = null;
  window.removeEventListener("resize", onWindowResized);
  document.removeEventListener("keydown", onKeyDown);
  popoverNode()?.removeEventListener("click", onBackdropClick);
  closeStationPopover();
  activeContainer = null;
  stations = [];
  sparklines = {};
  generation += 1;
}

// --- skeleton --------------------------------------------------------------

function buildSkeleton() {
  const view = el("div", "groundwater-view");
  const split = el("div", "groundwater-split");
  split.append(mapCard(), sideColumn());
  view.append(split, el("p", "groundwater-attribution", ATTRIBUTION));
  return view;
}

function iconButton(className, label, title, onTap) {
  const button = el("button", className, label);
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  button.addEventListener("click", onTap);
  return button;
}

function mapCard() {
  const card = el("div", "groundwater-map-card");
  const head = el("div", "groundwater-map-head");
  head.append(el("h2", "groundwater-map-title", "Grundwasser München"));
  const controls = el("div", "groundwater-map-controls");
  controls.append(
    iconButton("groundwater-btn groundwater-zoom-out", "−", "Verkleinern", () => changeZoom(-1)),
    iconButton("groundwater-btn groundwater-zoom-in", "+", "Vergrößern", () => changeZoom(1)),
  );
  head.append(controls);
  card.append(head);

  const map = el("div", "groundwater-map");
  map.append(el("div", "groundwater-tiles"));
  const markers = document.createElementNS(SVG_NS, "svg");
  markers.setAttribute("class", "groundwater-markers");
  markers.setAttribute("aria-label", "Messstellen");
  map.append(markers);
  map.append(el("p", "groundwater-hint", "Messstellen werden geladen …"));
  card.append(map);
  return card;
}

function sideColumn() {
  const side = el("div", "groundwater-side");
  side.append(el("h3", "groundwater-side-title", "Verlauf der Marker"));
  side.append(periodButtons());
  // Without this line the curves simply look missing: in the overview the
  // map shows dots only (density rule, see groundwater-map.js).
  side.append(
    el(
      "p",
      "groundwater-note groundwater-zoom-note",
      "Die Mini-Kurven erscheinen beim Hineinzoomen (+).",
    ),
  );
  side.append(
    el(
      "p",
      "groundwater-note",
      "Jede Mini-Kurve hat ihre eigene Skala – sie zeigt, ob eine Messstelle " +
        "steigt oder fällt, nicht wie hoch sie liegt.",
    ),
  );
  side.append(el("h3", "groundwater-side-title", "Niedrigwasser-Einstufung"));
  side.append(legend());
  side.append(el("p", "groundwater-count", ""));
  return side;
}

function periodButtons() {
  const bar = el("div", "groundwater-periods");
  for (const period of PERIODS) {
    const button = el("button", "groundwater-btn groundwater-period-btn", period.label);
    button.type = "button";
    if (period.days === selectedDays) button.classList.add("groundwater-period-active");
    button.addEventListener("click", () => selectPeriod(period.days));
    bar.append(button);
  }
  return bar;
}

function legend() {
  const list = el("ul", "groundwater-legend");
  for (const entry of legendEntries()) {
    const item = el("li", "groundwater-legend-item");
    const dot = el("span", "groundwater-legend-dot");
    dot.style.background = cssColor(entry.colorVar, "#888");
    item.append(dot, el("span", "groundwater-legend-label", entry.label));
    list.append(item);
  }
  return list;
}

// --- loading ---------------------------------------------------------------

async function loadAll() {
  const token = generation;
  // Settled, not all: a broken sparkline endpoint must still leave a map
  // full of stations, and vice versa — the failure of one source never
  // takes the whole view with it (same rule as the weather view).
  const [stationResult, sparkResult] = await Promise.allSettled([
    fetchGroundwaterStations(),
    fetchGroundwaterSparklines(selectedDays),
  ]);
  if (token !== generation || activeContainer === null) return;
  if (stationResult.status === "fulfilled" && Array.isArray(stationResult.value)) {
    stations = stationResult.value;
    hideHint();
  } else if (stations.length === 0) {
    showHint(errorMessage(stationResult, "Messstellen konnten nicht geladen werden."));
  }
  sparklines = sparkResult.status === "fulfilled" ? (sparkResult.value ?? {}) : {};
  renderCount();
  rebuildMap();
}

async function loadSparklines() {
  const token = generation;
  try {
    const series = await fetchGroundwaterSparklines(selectedDays);
    if (token !== generation || activeContainer === null) return;
    sparklines = series ?? {};
  } catch {
    if (token !== generation || activeContainer === null) return;
    // Markers without curves are still a usable map.
    sparklines = {};
  }
  renderMarkers();
}

function errorMessage(result, fallback) {
  const message = result.status === "rejected" ? result.reason?.message : null;
  return typeof message === "string" && message !== "" ? message : fallback;
}

function selectPeriod(days) {
  if (days === selectedDays || activeContainer === null) return;
  selectedDays = days;
  const bar = activeContainer.querySelector(".groundwater-periods");
  if (bar !== null) bar.replaceWith(periodButtons());
  loadSparklines();
}

function changeZoom(delta) {
  const next = stepGroundwaterZoom(zoomLevel, delta);
  if (next === zoomLevel) return;
  zoomLevel = next;
  rebuildMap();
}

// --- map -------------------------------------------------------------------

function mapElement() {
  return activeContainer === null ? null : activeContainer.querySelector(".groundwater-map");
}

function showHint(message) {
  const hint = activeContainer?.querySelector(".groundwater-hint");
  if (hint === undefined || hint === null) return;
  hint.textContent = message;
  hint.hidden = false;
}

function hideHint() {
  const hint = activeContainer?.querySelector(".groundwater-hint");
  if (hint !== undefined && hint !== null) hint.hidden = true;
}

function renderCount() {
  const label = activeContainer?.querySelector(".groundwater-count");
  if (label !== undefined && label !== null) {
    label.textContent = stationCountLabel(stations.length);
  }
}

/** Size of the map viewport in CSS pixels (square, see applyAutoFit). */
function viewportSize() {
  const map = mapElement();
  const width = map?.clientWidth || 0;
  const height = map?.clientHeight || 0;
  return {
    width: width > 0 ? width : FALLBACK_MAP_PX,
    height: height > 0 ? height : FALLBACK_MAP_PX,
  };
}

/**
 * Make the map square inside the room its column offers (kiosk only — see
 * AUTOFIT_MIN_WIDTH). The card is stretched to the full split height, so
 * its offsetHeight says nothing about its chrome; the space left for the
 * map is measured from the card's inner height minus padding, head and gap
 * (the same measurement the weather view had to learn).
 *
 * Returns true when the size changed, so the caller can rebuild the
 * pixel-placed tiles. Below the breakpoint the inline size is cleared and
 * the CSS aspect-ratio takes over.
 */
function applyAutoFit() {
  const map = mapElement();
  const card = activeContainer?.querySelector(".groundwater-map-card");
  const head = activeContainer?.querySelector(".groundwater-map-head");
  if (map === null || card === undefined || card === null) return false;

  const previous = map.style.height;
  if (window.innerWidth < AUTOFIT_MIN_WIDTH) {
    map.style.height = "";
    map.style.width = "";
    return previous !== "";
  }
  const styles = getComputedStyle(card);
  const padding = (parseFloat(styles.paddingTop) || 0) + (parseFloat(styles.paddingBottom) || 0);
  const gap = parseFloat(styles.rowGap) || 0;
  const headHeight = head === undefined || head === null ? 0 : head.offsetHeight;
  const room = card.clientHeight - padding - headHeight - gap;
  const available = Math.min(card.clientWidth || MAP_MIN_PX, room);
  const size = Math.max(MAP_MIN_PX, mapSize(available));
  map.style.width = `${size}px`;
  map.style.height = `${size}px`;
  return map.style.height !== previous;
}

function tileImage(url, tile) {
  const image = document.createElement("img");
  image.className = "groundwater-tile";
  image.src = url;
  image.alt = "";
  image.decoding = "async";
  image.style.left = `${tile.left}px`;
  image.style.top = `${tile.top}px`;
  image.style.width = `${tile.size}px`;
  image.style.height = `${tile.size}px`;
  return image;
}

/** Re-lay the base map tiles and the marker overlay for the current state. */
function rebuildMap() {
  if (activeContainer === null) return;
  applyAutoFit();
  const tiles = activeContainer.querySelector(".groundwater-tiles");
  if (tiles === null) return;
  const { width, height } = viewportSize();
  tiles.replaceChildren(
    ...viewportTiles(zoomLevel, MAP_TILE_PX, width, height).map((tile) =>
      tileImage(baseTileUrl(tile.zoom, tile.x, tile.y), tile),
    ),
  );
  renderMarkers();
}

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

// Resolve a CSS custom property to a concrete colour: SVG presentation
// attributes do not inherit CSS variables reliably.
function cssColor(cssVar, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
  return value || fallback;
}

function stationByNumber(number) {
  return stations.find((station) => station.number === number) ?? null;
}

/**
 * One marker: the dot on the well, the sparkline card above it, a hit area.
 *
 * Below SPARKLINE_MIN_ZOOM the card and its curve are left out entirely
 * (see the density rule in groundwater-map.js) and the dot grows to
 * OVERVIEW_DOT_RADIUS, because its colour is then the whole message.
 */
function markerNode(marker, station, colors) {
  const color = colors[situationColorVar(station.situation_class)];
  const group = svgEl("g", {
    class: "groundwater-marker",
    "data-number": station.number,
    tabindex: "0",
    role: "button",
  });
  // A foreign string, but an attribute value — not an HTML sink.
  group.setAttribute("aria-label", station.name);

  if (marker.showsCard) {
    group.append(
      svgEl("rect", {
        class: "groundwater-card",
        x: marker.cardX,
        y: marker.cardY,
        width: CARD_WIDTH,
        height: CARD_HEIGHT,
        rx: 4,
        fill: colors.surface,
        "fill-opacity": 0.85,
        stroke: color,
        "stroke-width": 1.5,
      }),
    );

    const values = sparklines[station.number];
    const polyline = sparklinePolyline(values, CARD_WIDTH - 8, CARD_HEIGHT - 6);
    if (polyline !== "") {
      group.append(
        svgEl("polyline", {
          class: "groundwater-spark",
          transform: `translate(${marker.cardX + 4} ${marker.cardY + 3})`,
          points: polyline,
          fill: "none",
          stroke: color,
          "stroke-width": 1.6,
          "stroke-linejoin": "round",
          "stroke-linecap": "round",
        }),
      );
    }
  }

  // Only the lowest card of a stack draws the dot — the stations share one
  // position, so a second dot would sit exactly on the first.
  if (marker.stackIndex === 0) {
    const radius = marker.showsCard ? DOT_RADIUS : OVERVIEW_DOT_RADIUS;
    group.append(
      svgEl("circle", {
        class: "groundwater-dot",
        cx: marker.x,
        cy: marker.y,
        r: station.situation_class === RECORD_LOW_CLASS ? radius + RECORD_LOW_DOT_BONUS : radius,
        fill: color,
        stroke: colors.surface,
        "stroke-width": 1.5,
      }),
    );
  }

  // A transparent rectangle over the marker: it gives the whole thing one
  // hit area instead of asking a finger to find a 1.6px line — and in the
  // overview it keeps the touch target at 44 px although the dot is 14.
  // Its shape (and the stacking that keeps two stations at one well head
  // apart) is pure geometry and lives in groundwater-map.js.
  group.append(
    svgEl("rect", {
      class: "groundwater-hit",
      x: marker.hit.x,
      y: marker.hit.y,
      width: marker.hit.width,
      height: marker.hit.height,
      fill: "transparent",
    }),
  );
  return group;
}

function renderMarkers() {
  if (activeContainer === null) return;
  const svg = activeContainer.querySelector(".groundwater-markers");
  if (svg === null) return;
  const { width, height } = viewportSize();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const colors = {
    surface: cssColor("--surface", "#fff"),
    "--ok-border": cssColor("--ok-border", "#16a34a"),
    "--warn-text": cssColor("--warn-text", "#92400e"),
    "--alert-border": cssColor("--alert-border", "#dc2626"),
    "--alert-text": cssColor("--alert-text", "#b91c1c"),
    "--text-muted": cssColor("--text-muted", "#6b7280"),
  };
  // ONE svg for all ~165 markers rather than 165 elements of their own —
  // the kiosk redraws this on every zoom step.
  const nodes = [];
  for (const marker of stationMarkers(stations, zoomLevel, MAP_TILE_PX, width, height)) {
    const station = stationByNumber(marker.number);
    if (station === null) continue;
    const node = markerNode(marker, station, colors);
    node.addEventListener("click", () => openStationPopover(marker.number, marker.siblings));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openStationPopover(marker.number, marker.siblings);
      }
    });
    nodes.push(node);
  }
  svg.replaceChildren(...nodes);
}

// --- detail popover --------------------------------------------------------

function popoverNode() {
  return document.getElementById("groundwater-popover");
}

export function closeStationPopover() {
  const node = popoverNode();
  if (node === null) return;
  node.hidden = true;
  node.replaceChildren();
  openStationNumber = null;
}

function metaRow(label, value) {
  const row = el("div", "groundwater-meta-row");
  row.append(el("span", "groundwater-meta-label", label));
  row.append(el("span", "groundwater-meta-value", value));
  return row;
}

function siblingSwitch(siblings, current) {
  const bar = el("div", "groundwater-siblings");
  bar.append(el("span", "groundwater-meta-label", "Weitere Messstelle an diesem Ort:"));
  for (const number of siblings) {
    if (number === current) continue;
    const other = stationByNumber(number);
    if (other === null) continue;
    const button = el("button", "groundwater-btn groundwater-sibling-btn", other.name);
    button.type = "button";
    button.addEventListener("click", () => openStationPopover(number, siblings));
    bar.append(button);
  }
  return bar;
}

function openStationPopover(number, siblings = []) {
  const node = popoverNode();
  const station = stationByNumber(number);
  if (node === null || station === null) return;
  openStationNumber = number;

  const panel = el("div", "popover-panel groundwater-detail");
  const header = el("div", "popover-header");
  // Foreign string from a scraped page — textContent only (el() does that).
  header.append(el("h2", "popover-title", station.name));
  const close = el("button", "popover-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Schließen");
  close.addEventListener("click", closeStationPopover);
  header.append(close);
  panel.append(header);

  const meta = el("div", "groundwater-meta");
  if (station.aquifer) meta.append(metaRow("Grundwasserleiter", station.aquifer));
  const tier = tierLabel(station.tier);
  if (tier) meta.append(metaRow("Stockwerk", tier));
  const level = formatLevel(station.level_m_nn);
  if (level) meta.append(metaRow("Grundwasserstand", level));
  // The depth stands here as a NUMBER on purpose: it runs opposite to the
  // level and is two orders of magnitude smaller, so it can never share the
  // curve's axis (see groundwater-chart.js).
  const depth = formatDepth(station.depth_m);
  if (depth) meta.append(metaRow("Flurabstand", depth));
  const measured = formatMeasuredAt(station.measured_at);
  if (measured) meta.append(metaRow("Messung", measured));
  meta.append(metaRow("Einstufung", situationLabel(station.situation, station.situation_class)));
  panel.append(meta);

  if (siblings.length > 1) panel.append(siblingSwitch(siblings, number));

  panel.append(el("h3", "groundwater-detail-title", "Verlauf (m ü. NN)"));
  const body = el("div", "groundwater-detail-body");
  body.append(el("p", "groundwater-detail-empty", "Verlauf wird geladen …"));
  panel.append(body);

  node.replaceChildren(panel);
  node.hidden = false;
  loadHistory(number);
}

async function loadHistory(number) {
  const token = generation;
  let points;
  try {
    points = await fetchGroundwaterHistory(number);
  } catch (error) {
    if (token !== generation || openStationNumber !== number) return;
    renderDetailMessage(error.message);
    return;
  }
  if (token !== generation || openStationNumber !== number) return;
  renderDetail(Array.isArray(points) ? points : []);
}

function detailBody() {
  return popoverNode()?.querySelector(".groundwater-detail-body") ?? null;
}

function renderDetailMessage(message) {
  const body = detailBody();
  if (body !== null) body.replaceChildren(el("p", "groundwater-detail-empty", message));
}

function renderDetail(points) {
  const body = detailBody();
  if (body === null) return;
  const axis = levelAxis(points);
  const bounds = timeBounds(points);
  if (axis === null || bounds === null) {
    renderDetailMessage("Für diese Messstelle liegt noch kein Verlauf vor.");
    return;
  }
  // Two passes, like the weather chart: hang an empty host first so the SVG
  // can be drawn into a viewBox that matches its real pixels (one user unit
  // = one CSS pixel, no stretched labels).
  const host = el("div", "groundwater-detail-chart");
  body.replaceChildren(host);
  const width = host.clientWidth || DETAIL_FALLBACK.width;
  const height = host.clientHeight || DETAIL_FALLBACK.height;
  host.replaceChildren(detailSvg(points, bounds, axis, width, height));
}

function detailText(x, y, text, anchor, fill) {
  const node = svgEl("text", {
    x,
    y,
    "text-anchor": anchor,
    fill,
    "font-size": DETAIL_TEXT_PX,
  });
  node.textContent = text;
  return node;
}

function detailSvg(points, bounds, axis, width, height) {
  const area = detailArea(width, height, DETAIL_PADDING);
  const svg = svgEl("svg", {
    class: "groundwater-detail-svg",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  const axisColor = cssColor("--border", "#ccc");
  const textColor = cssColor("--text-muted", "#888");
  const lineColor = cssColor("--accent", "#2563eb");

  for (const tick of levelTicks(axis, area)) {
    svg.append(
      svgEl("line", {
        x1: area.x,
        y1: tick.y,
        x2: area.x + area.width,
        y2: tick.y,
        stroke: axisColor,
        "stroke-width": 1,
      }),
    );
    svg.append(detailText(area.x - 8, tick.y + 4, formatAxisLevel(tick.value), "end", textColor));
  }

  // Three date labels (start, middle, end) — enough to place the curve in
  // time without crowding a popover-sized chart.
  const labelY = area.y + area.height + 20;
  const middle = bounds.minT + (bounds.maxT - bounds.minT) / 2;
  svg.append(detailText(area.x, labelY, formatAxisDay(bounds.minT), "start", textColor));
  if (bounds.maxT > bounds.minT) {
    svg.append(
      detailText(
        scaleTime(middle, bounds, area),
        labelY,
        formatAxisDay(middle),
        "middle",
        textColor,
      ),
    );
    svg.append(
      detailText(area.x + area.width, labelY, formatAxisDay(bounds.maxT), "end", textColor),
    );
  }

  const path = levelPath(points, bounds, axis, area);
  if (path !== "") {
    svg.append(
      svgEl("path", {
        class: "groundwater-detail-line",
        d: path,
        fill: "none",
        stroke: lineColor,
        "stroke-width": 2.5,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      }),
    );
  }
  return svg;
}

// --- events ----------------------------------------------------------------

function onKeyDown(event) {
  if (event.key === "Escape") closeStationPopover();
}

function onBackdropClick(event) {
  if (event.target === popoverNode()) closeStationPopover();
}

// Debounced: a resize drag would otherwise rebuild (and re-request) the
// whole tile set on every intermediate size.
function onWindowResized() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (activeContainer === null) return;
    rebuildMap();
  }, RESIZE_DEBOUNCE_MS);
}
