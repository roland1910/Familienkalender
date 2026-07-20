// Per-device persistence of the "slideshow as screensaver" toggle in
// localStorage. Like view-memory.js, each device/browser keeps its own
// setting: the kiosk display turns it on, a phone or browser leaves it off.
//
// Priority of the effective state (Etappe 29, mirrors resolveInitialView
// in view-memory.js): (a) an explicit per-device choice always wins,
// (b) else the server default from GET /api/config (settings key
// screensaver_default — the kiosk browser loses its localStorage on every
// restart), (c) else OFF: the screensaver never starts on a family
// member's phone by accident. Both inputs are untrusted (localStorage /
// network payload); anything unknown reads as "no choice" resp. OFF.

export const STORAGE_KEY = "familienkalender.screensaver.v1";

// The stored per-device choice: true (ON), false (OFF) or null when the
// device has never made one (or storage is unavailable/holds junk).
export function loadScreensaverChoice(storage = globalThis.localStorage) {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (raw === "1") return true;
    if (raw === "0") return false;
    return null;
  } catch {
    // Storage disabled (private mode, embedded webview) — no choice.
    return null;
  }
}

// The effective toggle state from device choice and server default. The
// server default is deliberately never persisted: only real user choices
// go to localStorage, so a later change of the server default still
// reaches devices without their own choice.
export function resolveScreensaverEnabled(deviceChoice, serverDefault) {
  if (deviceChoice === true || deviceChoice === false) return deviceChoice;
  return serverDefault === "on";
}

export function saveScreensaverEnabled(enabled, storage = globalThis.localStorage) {
  try {
    storage.setItem(STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Best effort: persistence failing must never break the calendar.
  }
}
