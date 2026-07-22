// Weather view: an animated rain radar over Munich on top, a Yr-style
// forecast chart (temperature line, precipitation bars, wind arrows)
// below. Radar frames come from /api/weather/radar/frames and are drawn
// from proxied map tiles; the forecast from /api/weather/forecast.
//
// All timers (forecast poll, radar poll, frame animation) are owned here
// and torn down by stopWeatherView(). Every dynamic string is rendered via
// textContent, and the SVG is built with createElementNS — never HTML.

import { baseTileUrl, fetchRadarFrames, fetchWeatherForecast, radarTileUrl } from "./api.js";
import { el } from "./dom.js";
import { setIcon } from "./icons.js";
import {
  CHART_HEIGHT,
  CHART_WIDTH,
  plotArea,
  precipBars,
  precipMax,
  precipTicks,
  sliceHours,
  tempBounds,
  tempPath,
  tempTicks,
  timeBounds,
  WIND_ARROW_OFFSET,
  WIND_ARROW_SIZE,
  WIND_LABEL_OFFSET,
  windArrowRotation,
  windSamples,
  X_LABEL_OFFSET,
  xTicks,
} from "./weather-chart.js";
import {
  formatAxisTime,
  formatFrameTime,
  formatPrecip,
  formatTemp,
  formatWind,
} from "./weather-format.js";
import {
  BASE_TILE_PX,
  baseZoomFor,
  DEFAULT_ZOOM,
  RADAR_TILE_PX,
  stepZoom,
  viewportTiles,
} from "./weather-map.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// MET Norway asks for polling no more often than the data changes; the
// backend caches for 30 minutes anyway, so this is the natural cadence.
const FORECAST_REFRESH_MS = 30 * 60 * 1000;
// New radar images appear every ~10 minutes; the backend caches 5.
const RADAR_REFRESH_MS = 5 * 60 * 1000;
// One animation step. Overridable for the E2E test, which cannot wait.
const FRAME_INTERVAL_MS = globalThis.WEATHER_FRAME_MS ?? 500;
// After the loop's last frame, hold it for a few extra ticks so the "now"
// image is readable before the animation jumps back to the start.
const LAST_FRAME_HOLD_TICKS = 3;
// Give up waiting for the tile preload rather than never animating.
const PRELOAD_TIMEOUT_MS = 8000;
// Viewport size assumed while the map element is not laid out yet.
const FALLBACK_MAP_WIDTH = 1200;
const FALLBACK_MAP_HEIGHT = 460;
// Debounce before rebuilding the tile layers for a new window size.
const RESIZE_DEBOUNCE_MS = 300;

// Auto-fit (Etappe 37): on the kiosk the whole weather view must fit the
// screen without scrolling — radar on top, forecast chart below. Above this
// viewport width we measure the available height and split the leftover
// (after the fixed chrome) between the radar map and the chart SVG. At or
// below it (the narrow HA ingress panel / a phone) we leave the natural
// clamp/aspect-ratio layout and allow scrolling instead.
const AUTOFIT_MIN_WIDTH = 901;
// Floors so neither half collapses to an unreadable sliver on a short kiosk.
const RADAR_MIN_PX = 200;
const CHART_MIN_PX = 180;
// The radar gets a little more than half — the map carries more detail.
const RADAR_SHARE = 0.54;

const PERIODS = [
  { hours: 24, label: "24 h" },
  { hours: 48, label: "48 h" },
  { hours: 96, label: "96 h" },
];
// 96 h by default (Etappe 37): Roland stands right in front of the kiosk when
// he opens the view and wants the wide outlook without tapping. 24 h and 48 h
// stay as switch options. MET delivers hourly points to ~63 h and 6-hourly
// after that; the chart scales its x-axis by real timestamp, so the uneven
// spacing of the back half lands time-correctly (see weather-chart.js).
const DEFAULT_HOURS = 96;

// Chart series colors, resolved from the theme variables at draw time.
const TEMP_COLOR_VAR = "--alert-border";
const PRECIP_COLOR_VAR = "--accent";

const ATTRIBUTION =
  "Daten: MET Norway / Yr · Radar: RainViewer · Karte: © OpenStreetMap-Mitwirkende";

let activeContainer = null;
let forecastTimer = null;
let radarTimer = null;
let animationTimer = null;
let selectedHours = DEFAULT_HOURS;
let zoomLevel = DEFAULT_ZOOM;
let playing = true;
let frames = [];
let frameIndex = 0;
// Ticks the newest frame has been held for (see LAST_FRAME_HOLD_TICKS).
let holdTicks = 0;
let lastForecast = null;
// Bumped whenever the view restarts or the tile layers are rebuilt, so a
// late image-preload callback from a previous state is ignored.
let generation = 0;

/** Start polling and rendering into `container` (idempotent). */
export function startWeatherView(container) {
  stopWeatherView();
  activeContainer = container;
  selectedHours = DEFAULT_HOURS;
  zoomLevel = DEFAULT_ZOOM;
  playing = true;
  frames = [];
  frameIndex = 0;
  holdTicks = 0;
  lastForecast = null;
  generation += 1;
  container.replaceChildren(buildSkeleton());
  loadForecast();
  loadRadar();
  forecastTimer = setInterval(loadForecast, FORECAST_REFRESH_MS);
  radarTimer = setInterval(loadRadar, RADAR_REFRESH_MS);
  // The tile layers are laid out in pixels, so a new window size needs a
  // rebuild (the kiosk never resizes, the ingress panel does).
  window.addEventListener("resize", onWindowResized);
}

/** Stop polling and animating (e.g. when switching back to the calendar). */
export function stopWeatherView() {
  for (const timer of [forecastTimer, radarTimer, animationTimer]) {
    if (timer !== null) clearInterval(timer);
  }
  forecastTimer = null;
  radarTimer = null;
  animationTimer = null;
  window.removeEventListener("resize", onWindowResized);
  clearTimeout(resizeTimer);
  activeContainer = null;
  frames = [];
  frameIndex = 0;
  holdTicks = 0;
  lastForecast = null;
  generation += 1;
}

// --- skeleton --------------------------------------------------------------

function buildSkeleton() {
  const view = el("div", "weather-view");
  view.append(radarCard(), chartCard(), el("p", "weather-attribution", ATTRIBUTION));
  return view;
}

function iconButton(className, label, title) {
  const button = el("button", className, label);
  button.type = "button";
  button.title = title;
  button.setAttribute("aria-label", title);
  return button;
}

// An <span class="btn-icon"> holding one of the inline SVG icons. The radar
// play/pause used the emoji ⏸/▶ before Etappe 38 — U+23F8 has no glyph in
// the kiosk browser's fonts, so it showed an empty box (see icons.js).
function iconSpan(name) {
  const span = el("span", "btn-icon");
  setIcon(span, name);
  return span;
}

function radarCard() {
  const card = el("div", "weather-radar");
  const head = el("div", "weather-radar-head");
  head.append(el("h2", "weather-radar-title", "Regenradar München"));

  const controls = el("div", "weather-radar-controls");
  controls.append(el("span", "weather-radar-time", ""));
  const play = iconButton("weather-radar-btn weather-radar-play", "", "Radar anhalten");
  play.append(iconSpan("pause"));
  play.addEventListener("click", togglePlay);
  controls.append(play);
  const out = iconButton("weather-radar-btn weather-zoom-out", "−", "Verkleinern");
  out.addEventListener("click", () => changeZoom(-1));
  const into = iconButton("weather-radar-btn weather-zoom-in", "+", "Vergrößern");
  into.addEventListener("click", () => changeZoom(1));
  controls.append(out, into);
  head.append(controls);
  card.append(head);

  const map = el("div", "weather-radar-map");
  map.append(el("div", "weather-radar-base"), el("div", "weather-radar-frames"));
  const hint = el("p", "weather-radar-hint", "Regenradar wird geladen …");
  map.append(hint);
  card.append(map);
  return card;
}

function chartCard() {
  const card = el("div", "weather-chart");
  const head = el("div", "weather-chart-head");
  head.append(el("h2", "weather-chart-title", "Vorhersage München"));
  head.append(chartLegend());
  card.append(head);
  card.append(periodButtons());
  card.append(el("div", "weather-chart-body"));
  return card;
}

function legendItem(cssVar, label) {
  const item = el("span", "weather-legend-item");
  const dot = el("span", "weather-legend-dot");
  dot.style.background = cssColor(cssVar, "#888");
  item.append(dot, el("span", "weather-legend-label", label));
  return item;
}

function chartLegend() {
  const legend = el("div", "weather-chart-legend");
  legend.append(legendItem(TEMP_COLOR_VAR, "Temperatur (°C)"));
  legend.append(legendItem(PRECIP_COLOR_VAR, "Niederschlag (mm)"));
  const wind = el("span", "weather-legend-item");
  wind.append(
    el("span", "weather-legend-arrow", "↑"),
    el("span", "weather-legend-label", "Wind (m/s)"),
  );
  legend.append(wind);
  return legend;
}

function periodButtons() {
  const bar = el("div", "weather-chart-periods");
  for (const period of PERIODS) {
    const button = el("button", "weather-period-btn", period.label);
    button.type = "button";
    if (period.hours === selectedHours) button.classList.add("weather-period-active");
    button.addEventListener("click", () => selectPeriod(period.hours));
    bar.append(button);
  }
  return bar;
}

function selectPeriod(hours) {
  if (hours === selectedHours || activeContainer === null) return;
  selectedHours = hours;
  const bar = activeContainer.querySelector(".weather-chart-periods");
  if (bar !== null) bar.replaceWith(periodButtons());
  renderChart(lastForecast);
}

// --- forecast chart --------------------------------------------------------

async function loadForecast() {
  const container = activeContainer;
  if (container === null) return;
  try {
    const payload = await fetchWeatherForecast();
    if (container !== activeContainer) return;
    lastForecast = payload.points;
    renderChart(lastForecast);
  } catch (error) {
    if (container !== activeContainer) return;
    // Keep an already drawn chart rather than replacing it with an error.
    if (lastForecast === null) renderChartMessage(error.message);
  }
}

function chartBody() {
  return activeContainer === null ? null : activeContainer.querySelector(".weather-chart-body");
}

function renderChartMessage(message) {
  const body = chartBody();
  if (body !== null) body.replaceChildren(el("p", "weather-chart-empty", message));
}

function renderChart(points) {
  const body = chartBody();
  if (body === null) return;
  if (points === null) {
    renderChartMessage("Vorhersage wird geladen …");
    return;
  }
  const visible = sliceHours(points, selectedHours, Date.now());
  const timeB = timeBounds(visible);
  const tempB = tempBounds(visible);
  if (timeB === null || tempB === null) {
    renderChartMessage("Keine Vorhersagedaten");
    return;
  }
  body.replaceChildren(chartSvg(visible, timeB, tempB));
  // The freshly inserted SVG is one of the two flexible parts — re-fit so
  // radar and chart share the kiosk height without scrolling. If the fit
  // changed the radar map's height, its pixel-placed tiles need rebuilding.
  if (applyWeatherAutoFit() && frames.length > 0) rebuildRadarLayers();
}

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

// Resolve a CSS custom property to a concrete color: SVG presentation
// attributes do not inherit CSS variables reliably.
function cssColor(cssVar, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(cssVar).trim();
  return value || fallback;
}

function svgText(x, y, text, { anchor = "middle", fill, size = 14, rotate = null } = {}) {
  const node = svgEl("text", {
    x,
    y,
    "text-anchor": anchor,
    fill,
    "font-size": size,
  });
  if (rotate !== null) node.setAttribute("transform", `rotate(${rotate} ${x} ${y})`);
  node.textContent = text;
  return node;
}

function chartSvg(points, timeB, tempB) {
  const area = plotArea();
  const svg = svgEl("svg", {
    class: "weather-chart-svg",
    viewBox: `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  const axisColor = cssColor("--border", "#ccc");
  const textColor = cssColor("--text-muted", "#888");
  const tempColor = cssColor(TEMP_COLOR_VAR, "#dc2626");
  const precipColor = cssColor(PRECIP_COLOR_VAR, "#2563eb");
  const maxMm = precipMax(points);

  // Gridlines with the temperature scale on the left and the matching
  // precipitation scale on the right (both use the same four steps).
  const tempRows = tempTicks(tempB, area, 4);
  const precipRows = precipTicks(maxMm, area, 4);
  tempRows.forEach((tick, index) => {
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
    svg.append(
      svgText(area.x - 10, tick.y + 5, formatTemp(tick.value), {
        anchor: "end",
        fill: tempColor,
      }),
    );
    const precipRow = precipRows[index];
    if (precipRow !== undefined) {
      svg.append(
        svgText(area.x + area.width + 10, precipRow.y + 5, formatPrecip(precipRow.value), {
          anchor: "start",
          fill: precipColor,
        }),
      );
    }
  });

  // Precipitation bars sit behind the temperature line.
  for (const bar of precipBars(points, timeB, maxMm, area)) {
    svg.append(
      svgEl("rect", {
        x: bar.x,
        y: bar.y,
        width: bar.width,
        height: bar.height,
        fill: precipColor,
        "fill-opacity": 0.55,
        rx: 2,
      }),
    );
  }

  const path = tempPath(points, timeB, tempB, area);
  if (path !== "") {
    svg.append(
      svgEl("path", {
        class: "weather-temp-line",
        d: path,
        fill: "none",
        stroke: tempColor,
        "stroke-width": 3,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      }),
    );
  }

  // Time axis.
  for (const tick of xTicks(timeB, area, 6)) {
    svg.append(
      svgText(
        tick.x,
        area.y + area.height + X_LABEL_OFFSET,
        formatAxisTime(tick.t, selectedHours),
        {
          fill: textColor,
        },
      ),
    );
  }

  // Wind row: an arrow pointing where the wind blows to, plus its speed.
  const arrowY = area.y + area.height + WIND_ARROW_OFFSET;
  const labelY = area.y + area.height + WIND_LABEL_OFFSET;
  for (const sample of windSamples(points, 8)) {
    const x = area.x + ((sample.t - timeB.minT) / (timeB.maxT - timeB.minT)) * area.width;
    svg.append(windArrow(x, arrowY, windArrowRotation(sample.wind_dir_deg), textColor));
    svg.append(svgText(x, labelY, formatWind(sample.wind_ms), { fill: textColor, size: 16 }));
  }
  return svg;
}

// A simple arrow glyph pointing up at rotation 0, rotated into the wind
// direction around its own centre.
function windArrow(x, y, rotation, color) {
  const size = WIND_ARROW_SIZE;
  const group = svgEl("g", { transform: `rotate(${rotation} ${x} ${y})` });
  group.append(
    svgEl("line", {
      x1: x,
      y1: y + size,
      x2: x,
      y2: y - size,
      stroke: color,
      "stroke-width": 2.5,
      "stroke-linecap": "round",
    }),
  );
  group.append(
    svgEl("polyline", {
      points: `${x - 6},${y - size + 7} ${x},${y - size} ${x + 6},${y - size + 7}`,
      fill: "none",
      stroke: color,
      "stroke-width": 2.5,
      "stroke-linejoin": "round",
      "stroke-linecap": "round",
    }),
  );
  return group;
}

// --- rain radar ------------------------------------------------------------

async function loadRadar() {
  const container = activeContainer;
  if (container === null) return;
  try {
    const list = await fetchRadarFrames();
    if (container !== activeContainer) return;
    frames = Array.isArray(list) ? list : [];
    if (frames.length === 0) {
      showRadarHint("Regenradar derzeit nicht verfügbar.");
      return;
    }
    rebuildRadarLayers();
  } catch (error) {
    if (container !== activeContainer) return;
    if (frames.length === 0) showRadarHint(error.message);
  }
}

function showRadarHint(message) {
  if (activeContainer === null) return;
  const hint = activeContainer.querySelector(".weather-radar-hint");
  if (hint === null) return;
  hint.textContent = message;
  hint.hidden = false;
}

function hideRadarHint() {
  if (activeContainer === null) return;
  const hint = activeContainer.querySelector(".weather-radar-hint");
  if (hint !== null) hint.hidden = true;
}

// One absolutely positioned tile image at its place in the viewport.
function tileImage(url, tile) {
  const image = document.createElement("img");
  image.className = "weather-tile";
  image.src = url;
  image.alt = "";
  image.decoding = "async";
  image.style.left = `${tile.left}px`;
  image.style.top = `${tile.top}px`;
  image.style.width = `${tile.size}px`;
  image.style.height = `${tile.size}px`;
  return image;
}

// Size of the radar viewport in CSS pixels. Falls back to a sensible
// default while the element has not been laid out yet (measuring a
// display:none section returns 0).
function viewportSize() {
  const map = activeContainer?.querySelector(".weather-radar-map");
  const width = map?.clientWidth || 0;
  const height = map?.clientHeight || 0;
  return {
    width: width > 0 ? width : FALLBACK_MAP_WIDTH,
    height: height > 0 ? height : FALLBACK_MAP_HEIGHT,
  };
}

// Size the radar map and the chart SVG so the whole view fits the visible
// area without scrolling (kiosk only; see AUTOFIT_MIN_WIDTH). The two are the
// only flexible parts: collapse them, measure the fixed chrome around them,
// then hand the leftover height back split by RADAR_SHARE. Returns true when
// the radar map height changed, so the caller can rebuild its pixel-placed
// tiles. On the narrow panel the inline heights are cleared, restoring the
// CSS clamp/aspect-ratio layout that is allowed to scroll.
function applyWeatherAutoFit() {
  if (activeContainer === null) return false;
  const section = document.getElementById("weather");
  const view = activeContainer.querySelector(".weather-view");
  const map = activeContainer.querySelector(".weather-radar-map");
  const svg = activeContainer.querySelector(".weather-chart-svg");
  if (section === null || view === null || map === null) return false;

  if (window.innerWidth < AUTOFIT_MIN_WIDTH) {
    const changed = map.style.height !== "";
    map.style.height = "";
    if (svg !== null) svg.style.height = "";
    return changed;
  }

  const previousMapHeight = map.style.height;
  // Collapse the flexible parts so `view.offsetHeight` is just the chrome.
  map.style.height = "0px";
  if (svg !== null) svg.style.height = "0px";
  const styles = getComputedStyle(section);
  const padding = (parseFloat(styles.paddingTop) || 0) + (parseFloat(styles.paddingBottom) || 0);
  const available = section.clientHeight - padding;
  const leftover = available - view.offsetHeight;
  if (leftover <= RADAR_MIN_PX + CHART_MIN_PX) {
    // Too little room to fit both — keep them collapsed would look broken, so
    // fall back to the floors (the view may then scroll, better than a sliver).
    map.style.height = `${RADAR_MIN_PX}px`;
    if (svg !== null) svg.style.height = `${CHART_MIN_PX}px`;
    return map.style.height !== previousMapHeight;
  }
  const radarHeight = Math.max(RADAR_MIN_PX, Math.round(leftover * RADAR_SHARE));
  const chartHeight = Math.max(CHART_MIN_PX, leftover - radarHeight);
  map.style.height = `${radarHeight}px`;
  if (svg !== null) svg.style.height = `${chartHeight}px`;
  return map.style.height !== previousMapHeight;
}

function radarFrameLayer(frame, tiles) {
  const layer = el("div", "weather-radar-frame");
  layer.append(
    ...tiles.map((tile) => tileImage(radarTileUrl(frame.id, tile.zoom, tile.x, tile.y), tile)),
  );
  return layer;
}

// Build the base map and one layer per radar frame for the current zoom
// and viewport size, then start animating once the images have loaded
// (no flicker). Also called after a resize, hence the full rebuild.
function rebuildRadarLayers() {
  if (activeContainer === null) return;
  stopAnimation();
  generation += 1;
  const token = generation;

  const base = activeContainer.querySelector(".weather-radar-base");
  const frameHost = activeContainer.querySelector(".weather-radar-frames");
  if (base === null || frameHost === null) return;

  // Size the radar map to its kiosk share first, so the tiles below are laid
  // out against the final height (idempotent — a no-op when already fitted).
  applyWeatherAutoFit();
  const { width, height } = viewportSize();
  // The base map is fetched one zoom deeper at normal tile size so it
  // stays sharp under the double-sized radar tiles (see weather-map.js).
  const baseTiles = viewportTiles(baseZoomFor(zoomLevel), BASE_TILE_PX, width, height);
  const radarTiles = viewportTiles(zoomLevel, RADAR_TILE_PX, width, height);
  base.replaceChildren(
    ...baseTiles.map((tile) => tileImage(baseTileUrl(tile.zoom, tile.x, tile.y), tile)),
  );

  const layers = frames.map((frame) => radarFrameLayer(frame, radarTiles));
  frameHost.replaceChildren(...layers);
  frameIndex = Math.min(frameIndex, layers.length - 1);
  showFrame(frameIndex);
  hideRadarHint();

  whenTilesSettled(frameHost, () => {
    // Ignore a preload that finished after a zoom change or a view switch.
    if (token !== generation || activeContainer === null) return;
    if (playing) startAnimation();
  });
}

// Resolve once every <img> below `root` has loaded or failed — a broken
// tile must not block the animation forever, hence the timeout as well.
function whenTilesSettled(root, done) {
  const images = [...root.querySelectorAll("img")];
  let pending = images.filter((image) => !image.complete).length;
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    done();
  };
  if (pending === 0) {
    finish();
    return;
  }
  const settle = () => {
    pending -= 1;
    if (pending <= 0) finish();
  };
  for (const image of images) {
    if (image.complete) continue;
    image.addEventListener("load", settle, { once: true });
    image.addEventListener("error", settle, { once: true });
  }
  setTimeout(finish, PRELOAD_TIMEOUT_MS);
}

function frameLayers() {
  if (activeContainer === null) return [];
  return [...activeContainer.querySelectorAll(".weather-radar-frame")];
}

function showFrame(index) {
  const layers = frameLayers();
  if (layers.length === 0) return;
  const bounded = ((index % layers.length) + layers.length) % layers.length;
  frameIndex = bounded;
  layers.forEach((layer, position) => {
    layer.classList.toggle("weather-radar-frame-active", position === bounded);
  });
  const label = activeContainer.querySelector(".weather-radar-time");
  const frame = frames[bounded];
  if (label !== null && frame !== undefined) label.textContent = formatFrameTime(frame.t);
}

function advanceFrame() {
  const layers = frameLayers();
  if (layers.length === 0) return;
  // Hold the newest image for a few extra ticks before looping back.
  const isLast = frameIndex === layers.length - 1;
  if (isLast && holdTicks < LAST_FRAME_HOLD_TICKS) {
    holdTicks += 1;
    return;
  }
  holdTicks = 0;
  showFrame(frameIndex + 1);
}

function startAnimation() {
  stopAnimation();
  if (frameLayers().length < 2) return;
  animationTimer = setInterval(advanceFrame, FRAME_INTERVAL_MS);
}

function stopAnimation() {
  if (animationTimer !== null) {
    clearInterval(animationTimer);
    animationTimer = null;
  }
}

function togglePlay() {
  if (activeContainer === null) return;
  playing = !playing;
  if (playing) startAnimation();
  else stopAnimation();
  const button = activeContainer.querySelector(".weather-radar-play");
  if (button === null) return;
  const title = playing ? "Radar anhalten" : "Radar abspielen";
  const slot = button.querySelector(".btn-icon");
  if (slot !== null) setIcon(slot, playing ? "pause" : "play");
  button.title = title;
  button.setAttribute("aria-label", title);
}

function changeZoom(delta) {
  const next = stepZoom(zoomLevel, delta);
  if (next === zoomLevel) return;
  zoomLevel = next;
  if (frames.length > 0) rebuildRadarLayers();
}

// Debounced: a resize drag would otherwise rebuild (and re-request) the
// whole tile set on every intermediate size.
let resizeTimer;
function onWindowResized() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (activeContainer === null) return;
    // Re-fit first (the available height changed); rebuilding the radar tiles
    // picks up the new map height. Without frames there is nothing to rebuild,
    // but the chart SVG still needs re-fitting.
    const changed = applyWeatherAutoFit();
    if (frames.length > 0 && changed) rebuildRadarLayers();
  }, RESIZE_DEBOUNCE_MS);
}
