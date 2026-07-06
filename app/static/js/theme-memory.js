// Per-device persistence of the UI theme in localStorage: each device
// (kiosk display, phone, browser) keeps its own light/dark preference.
//
// Three states: "auto" follows the system/HA theme via the CSS media query
// (prefers-color-scheme), "light" and "dark" force that theme regardless of
// the system setting. Default is "auto".
//
// Robustness rule (like view-memory.js): localStorage is world-writable from
// the page origin, so any stored value is untrusted input. Anything that is
// not one of the three known themes falls back to "auto" — never a crash.

export const STORAGE_KEY = "familienkalender.theme.v1";

export const THEMES = ["auto", "light", "dark"];

// Validates a raw value against the known themes; anything else -> "auto".
export function normalizeTheme(value) {
  return THEMES.includes(value) ? value : "auto";
}

// The next theme in the cycle auto -> light -> dark -> auto, used by the
// header toggle button.
export function nextTheme(theme) {
  const index = THEMES.indexOf(normalizeTheme(theme));
  return THEMES[(index + 1) % THEMES.length];
}

export function loadTheme(storage = globalThis.localStorage) {
  try {
    return normalizeTheme(storage.getItem(STORAGE_KEY));
  } catch {
    // Storage disabled (private mode, embedded webview) — default "auto".
    return "auto";
  }
}

export function saveTheme(theme, storage = globalThis.localStorage) {
  try {
    storage.setItem(STORAGE_KEY, normalizeTheme(theme));
  } catch {
    // Best effort: persistence failing must never break the calendar.
  }
}
