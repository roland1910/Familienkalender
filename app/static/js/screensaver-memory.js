// Per-device persistence of the "slideshow as screensaver" toggle in
// localStorage. Like view-memory.js, each device/browser keeps its own
// setting: the kiosk display turns it on, a phone or browser leaves it off.
//
// Default OFF: the screensaver only starts where an admin explicitly
// enabled it, never on a family member's phone. Any non-boolean stored
// value is treated as OFF (untrusted input, never a crash).

export const STORAGE_KEY = "familienkalender.screensaver.v1";

export function loadScreensaverEnabled(storage = globalThis.localStorage) {
  try {
    return storage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Storage disabled (private mode, embedded webview) — default OFF.
    return false;
  }
}

export function saveScreensaverEnabled(enabled, storage = globalThis.localStorage) {
  try {
    storage.setItem(STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Best effort: persistence failing must never break the calendar.
  }
}
