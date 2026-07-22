// Power view: a compact one-line summary of the current values, a
// production-vs-consumption history chart (inline SVG), and the per-device
// list. Live values come from /api/power (polled every 15s); the chart from
// /api/power/history (polled every 60s, matching the HA dashboard cadence).
// Both timers are owned here and torn down by stopPowerView(). All dynamic
// values are rendered via textContent; the SVG is built with createElementNS.

import { deviceDisplayName } from "../admin/power-devices.js";
import { fetchPower, fetchPowerHistory } from "./api.js";
import { el } from "./dom.js";
import {
  CHART_HEIGHT,
  CHART_WIDTH,
  computeBounds,
  linePath,
  plotArea,
  xTicks,
  yTicks,
} from "./power-chart.js";
import { balanceTile, formatChartTime, formatLastUpdated, formatWatts } from "./power-format.js";

// Axis label size in the chart's user units (Etappe 39: one step down,
// matching the --fs-* scale in calendar.css).
const CHART_TEXT_PX = 13;

const REFRESH_INTERVAL_MS = 15000;
const HISTORY_REFRESH_INTERVAL_MS = 60000;
const SVG_NS = "http://www.w3.org/2000/svg";

// Period buttons, mirroring the HA dashboard's 1T/3T/1W. Default is 1 day.
const PERIODS = [
  { hours: 24, label: "1T" },
  { hours: 72, label: "3T" },
  { hours: 168, label: "1W" },
];
const DEFAULT_HOURS = 24;

// Chart series: production green (--ok), consumption amber/accent (--warn).
const SERIES = [
  { key: "production", label: "Erzeugung", cssVar: "--ok-border" },
  { key: "consumption", label: "Verbrauch", cssVar: "--warn-text" },
];

let timer = null;
let historyTimer = null;
let activeContainer = null;
let selectedHours = DEFAULT_HOURS;
// The last rendered live snapshot, kept so a chart refresh (or period switch)
// re-renders without a flash of the summary line.
let lastPayload = null;

/** Start polling and rendering into `container` (idempotent). */
export function startPowerView(container) {
  stopPowerView();
  activeContainer = container;
  selectedHours = DEFAULT_HOURS;
  lastPayload = null;
  loadPower();
  loadHistory();
  timer = setInterval(loadPower, REFRESH_INTERVAL_MS);
  historyTimer = setInterval(loadHistory, HISTORY_REFRESH_INTERVAL_MS);
}

/** Stop polling (e.g. when switching back to the calendar). */
export function stopPowerView() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  if (historyTimer !== null) {
    clearInterval(historyTimer);
    historyTimer = null;
  }
  activeContainer = null;
  lastPayload = null;
}

async function loadPower() {
  const container = activeContainer;
  if (container === null) return;
  let payload;
  try {
    payload = await fetchPower();
  } catch (error) {
    if (container === activeContainer) renderPowerError(container, error.message);
    return;
  }
  // Ignore responses that arrive after the view was switched away.
  if (container === activeContainer) renderPowerView(container, payload);
}

async function loadHistory() {
  const container = activeContainer;
  if (container === null) return;
  const hours = selectedHours;
  let payload;
  try {
    payload = await fetchPowerHistory(hours);
  } catch {
    payload = null; // The chart shows a muted hint; live values stay up.
  }
  // Only apply if still active and the window has not changed meanwhile.
  if (container === activeContainer && hours === selectedHours) {
    renderChart(container, payload);
  }
}

// Small, muted "as of" time for a value; nothing rendered when the state
// carries no last_updated.
function lastUpdatedNote(metric, now, className) {
  const text = formatLastUpdated(metric.last_updated, now);
  if (text === "") return null;
  return el("span", className, text);
}

// One "Label X W" segment of the compact summary line.
function summarySegment(label, metric, stateClass) {
  const segment = el("span", "power-summary-segment");
  if (stateClass) segment.classList.add(`power-summary-${stateClass}`);
  segment.append(el("span", "power-summary-label", label));
  const value = el("span", "power-summary-value", formatWatts(metric.value));
  if (!metric.available) {
    value.classList.add("power-unavailable");
    value.append(el("span", "power-unavailable-note", "nicht verfügbar"));
  }
  segment.append(value);
  return segment;
}

// The compact summary: Erzeugung · Verbrauch · balance, on one line.
function summaryLine(payload, now) {
  const line = el("div", "power-summary");
  line.append(summarySegment("Erzeugung", payload.production));
  line.append(summarySegment("Verbrauch", payload.consumption));
  const balance = balanceTile(payload.surplus, payload.grid_import);
  const balanceMetric = {
    value: balance.value,
    available: balance.available,
  };
  line.append(summarySegment(balance.label, balanceMetric, balance.state));
  // One muted freshness note for the whole line, from the production sensor.
  const stamp = lastUpdatedNote(payload.production, now, "power-summary-time");
  if (stamp !== null) line.append(stamp);
  return line;
}

function deviceRow(device, now) {
  const row = el("li", "power-device");
  row.append(
    el(
      "span",
      "power-device-name",
      deviceDisplayName(device.name, device.friendly_name, device.entity_id),
    ),
  );
  const valueBox = el("span", "power-device-value");
  const value = el("span", "power-device-watts", formatWatts(device.value));
  if (!device.available) {
    value.classList.add("power-unavailable");
    value.append(el("span", "power-unavailable-note", "nicht verfügbar"));
  }
  valueBox.append(value);
  const stamp = lastUpdatedNote(device, now, "power-device-time");
  if (stamp !== null) valueBox.append(stamp);
  row.append(valueBox);
  return row;
}

// --- SVG chart -------------------------------------------------------------

function svgEl(name, attrs = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

// Resolve a CSS custom property to a concrete color for SVG stroke/fill
// (SVG presentation attributes do not inherit CSS variables reliably).
function cssColor(cssVar, fallback) {
  const root = document.documentElement;
  const value = getComputedStyle(root).getPropertyValue(cssVar).trim();
  return value || fallback;
}

function chartLegend() {
  const legend = el("div", "power-chart-legend");
  for (const series of SERIES) {
    const item = el("span", "power-chart-legend-item");
    const dot = el("span", "power-chart-legend-dot");
    dot.style.background = cssColor(series.cssVar, "#888");
    item.append(dot, el("span", "power-chart-legend-label", series.label));
    legend.append(item);
  }
  return legend;
}

function periodButtons() {
  const bar = el("div", "power-chart-periods");
  for (const period of PERIODS) {
    const button = el("button", "power-period-btn", period.label);
    button.type = "button";
    if (period.hours === selectedHours) button.classList.add("power-period-active");
    button.addEventListener("click", () => selectPeriod(period.hours));
    bar.append(button);
  }
  return bar;
}

function selectPeriod(hours) {
  if (hours === selectedHours) return;
  selectedHours = hours;
  // Re-render live values (updates the active button) and reload the chart.
  if (activeContainer !== null && lastPayload !== null) {
    renderPowerView(activeContainer, lastPayload);
  }
  loadHistory();
}

// Build the inline SVG for the history payload, or a muted empty hint.
function chartSvg(history) {
  const production = history ? history.production : [];
  const consumption = history ? history.consumption : [];
  const bounds = computeBounds([production, consumption]);
  if (bounds === null) {
    const hint = el("p", "power-chart-empty", "Keine Verlaufsdaten");
    return hint;
  }
  const area = plotArea();
  const svg = svgEl("svg", {
    class: "power-chart-svg",
    viewBox: `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`,
    preserveAspectRatio: "none",
    role: "img",
  });

  const axisColor = cssColor("--border", "#ccc");
  const textColor = cssColor("--text-muted", "#888");

  // Horizontal gridlines + y labels (Watt).
  for (const tick of yTicks(bounds, area, 4)) {
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
    const label = svgEl("text", {
      x: area.x - 8,
      y: tick.y + 4,
      "text-anchor": "end",
      fill: textColor,
      "font-size": CHART_TEXT_PX,
    });
    label.textContent = formatWatts(tick.value);
    svg.append(label);
  }

  // Vertical x-axis ticks (time labels).
  for (const tick of xTicks(bounds, area, 5)) {
    const label = svgEl("text", {
      x: tick.x,
      y: area.y + area.height + 20,
      "text-anchor": "middle",
      fill: textColor,
      "font-size": CHART_TEXT_PX,
    });
    label.textContent = formatChartTime(tick.t, selectedHours);
    svg.append(label);
  }

  // Series lines.
  const points = { production, consumption };
  for (const series of SERIES) {
    const d = linePath(points[series.key], bounds, area);
    if (d === "") continue;
    svg.append(
      svgEl("path", {
        d,
        fill: "none",
        stroke: cssColor(series.cssVar, "#888"),
        "stroke-width": 2.5,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
      }),
    );
  }
  return svg;
}

// Render (or re-render) just the chart body into the existing chart card.
function renderChart(container, history) {
  const body = container.querySelector(".power-chart-body");
  if (body === null) return;
  body.replaceChildren(chartSvg(history));
}

function chartCard(history) {
  const card = el("div", "power-chart");
  const head = el("div", "power-chart-head");
  head.append(el("h2", "power-chart-title", "Erzeugung vs. Verbrauch"));
  head.append(chartLegend());
  card.append(head);
  card.append(periodButtons());
  const body = el("div", "power-chart-body");
  body.append(chartSvg(history));
  card.append(body);
  return card;
}

/** Render a fresh live snapshot (exported for potential reuse; no timer logic). */
export function renderPowerView(container, payload, now = new Date()) {
  lastPayload = payload;
  // Preserve any chart already rendered so a live-value refresh does not
  // wipe it (the chart refreshes on its own, slower cadence).
  const existingBody = container.querySelector(".power-chart-body");
  const existingChart = existingBody ? existingBody.firstChild : null;

  const view = el("div", "power-view");
  view.append(summaryLine(payload, now));
  const card = chartCard(null);
  if (existingChart !== null) {
    card.querySelector(".power-chart-body").replaceChildren(existingChart);
  }
  view.append(card);
  if (payload.devices.length > 0) {
    view.append(el("h2", "power-devices-title", "Geräte"));
    const list = el("ul", "power-devices");
    for (const device of payload.devices) list.append(deviceRow(device, now));
    view.append(list);
  }
  container.replaceChildren(view);
}

/** Error state with the German message from the backend; keeps polling. */
export function renderPowerError(container, message) {
  const view = el("div", "power-view");
  const box = el("div", "power-error");
  box.append(el("p", "power-error-title", "Stromdaten nicht verfügbar"));
  box.append(el("p", "power-error-message", message));
  view.append(box);
  container.replaceChildren(view);
}
