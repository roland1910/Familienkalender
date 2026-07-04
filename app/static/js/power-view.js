// Power view: big tiles for production/consumption/balance plus the
// per-device list. Data comes from /api/power; the view polls every 15s
// while it is active (timer started/stopped by the mode switch in main.js).
// All dynamic values are rendered via textContent.

import { fetchPower } from "./api.js";
import { el } from "./dom.js";
import { balanceTile, formatWatts } from "./power-format.js";

const REFRESH_INTERVAL_MS = 15000;

const BALANCE_ICONS = {
  surplus: "☀️",
  grid: "🔌",
  balanced: "⚖️",
};

let timer = null;
let activeContainer = null;

/** Start polling and rendering into `container` (idempotent). */
export function startPowerView(container) {
  stopPowerView();
  activeContainer = container;
  loadPower();
  timer = setInterval(loadPower, REFRESH_INTERVAL_MS);
}

/** Stop polling (e.g. when switching back to the calendar). */
export function stopPowerView() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  activeContainer = null;
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

function metricValue(metric) {
  const value = el("p", "power-tile-value", formatWatts(metric.value));
  if (!metric.available) {
    value.classList.add("power-unavailable");
    value.append(el("span", "power-unavailable-note", "nicht verfügbar"));
  }
  return value;
}

function tile(icon, label, metric, stateClass) {
  const node = el("div", "power-tile");
  if (stateClass) node.classList.add(`power-tile-${stateClass}`);
  node.append(el("p", "power-tile-icon", icon), el("p", "power-tile-label", label));
  node.append(metricValue(metric));
  return node;
}

function deviceRow(device) {
  const row = el("li", "power-device");
  row.append(el("span", "power-device-name", device.name));
  const value = el("span", "power-device-value", formatWatts(device.value));
  if (!device.available) {
    value.classList.add("power-unavailable");
    value.append(el("span", "power-unavailable-note", "nicht verfügbar"));
  }
  row.append(value);
  return row;
}

/** Render a fresh snapshot (exported for potential reuse; no timer logic). */
export function renderPowerView(container, payload) {
  const view = el("div", "power-view");
  const tiles = el("div", "power-tiles");
  tiles.append(tile("☀️", "Erzeugung", payload.production));
  tiles.append(tile("🏠", "Verbrauch", payload.consumption));
  const balance = balanceTile(payload.surplus, payload.grid_import);
  tiles.append(
    tile(
      BALANCE_ICONS[balance.state],
      balance.label,
      {
        value: balance.value,
        available: balance.available,
      },
      balance.state,
    ),
  );
  view.append(tiles);
  if (payload.devices.length > 0) {
    view.append(el("h2", "power-devices-title", "Geräte"));
    const list = el("ul", "power-devices");
    for (const device of payload.devices) list.append(deviceRow(device));
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
