// Per-device persistence of the UI position in localStorage: the active
// view (month/week), the displayed period (anchor day) and the mode
// (calendar/power/weather) survive a reload. Kiosk and browser each keep
// their own state — localStorage is per device/browser by design.
//
// Robustness rule: localStorage is world-writable from the page origin,
// so every stored value is treated as untrusted input. Anything that does
// not validate (foreign JSON, wrong enums, impossible dates) yields null
// and the app starts with its defaults — never a crash.

import { fromISODate, toISODate } from "./dates.js";

export const STORAGE_KEY = "familienkalender.view-state.v1";

const VIEWS = new Set(["month", "week"]);
const MODES = new Set(["calendar", "power", "weather"]);
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function serializeViewState({ view, anchor, mode }) {
  return JSON.stringify({ view, anchor: toISODate(anchor), mode });
}

// Returns { view, anchor: Date, mode } or null if the value is invalid.
export function deserializeViewState(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const { view, anchor, mode } = parsed;
  if (!VIEWS.has(view) || !MODES.has(mode)) return null;
  if (typeof anchor !== "string" || !ISO_DATE_PATTERN.test(anchor)) return null;
  const day = fromISODate(anchor);
  // The round trip rejects impossible dates like 2026-02-31, which the
  // Date constructor would otherwise silently roll over into March.
  if (Number.isNaN(day.getTime()) || toISODate(day) !== anchor) return null;
  return { view, anchor: day, mode };
}

// Priority of the initial calendar view: (a) a valid per-device choice
// (from the persisted view state) always wins, (b) else the server-side
// default from GET /api/config (settings key default_view — the kiosk
// browser loses its localStorage on every restart), (c) else "month".
// Both inputs are untrusted (localStorage / network payload) and are
// validated against the known views.
export function resolveInitialView(savedView, serverDefault) {
  if (VIEWS.has(savedView)) return savedView;
  if (VIEWS.has(serverDefault)) return serverDefault;
  return "month";
}

export function loadViewState(storage = globalThis.localStorage) {
  try {
    return deserializeViewState(storage.getItem(STORAGE_KEY));
  } catch {
    // Storage disabled (private mode, embedded webview) — use defaults.
    return null;
  }
}

export function saveViewState(state, storage = globalThis.localStorage) {
  try {
    storage.setItem(STORAGE_KEY, serializeViewState(state));
  } catch {
    // Best effort: persistence failing (quota, disabled storage) must
    // never break the calendar itself.
  }
}
