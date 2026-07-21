// Pure weather-view formatting (no DOM), unit-testable with node --test.
// German display strings, local time — like the rest of the frontend.

function pad2(number) {
  return String(number).padStart(2, "0");
}

const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

/**
 * X-axis tick label for a forecast timestamp (epoch ms), local time.
 * A 24h window shows just "HH:MM"; the 48h window prefixes the weekday
 * so the second day is distinguishable ("Mi 14:00").
 */
export function formatAxisTime(ms, hours) {
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  const time = `${pad2(moment.getHours())}:${pad2(moment.getMinutes())}`;
  if (hours <= 24) return time;
  return `${WEEKDAYS[moment.getDay()]} ${time}`;
}

/** Temperature axis label, e.g. 12.5 → "13°". */
export function formatTemp(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return `${Math.round(value)}°`;
}

/** Precipitation axis label, e.g. 1.5 → "1,5 mm" (German decimal comma). */
export function formatPrecip(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  const rounded = Math.round(value * 10) / 10;
  return `${rounded.toLocaleString("de-DE", { maximumFractionDigits: 1 })} mm`;
}

/** Wind speed label, e.g. 3.4 → "3". Whole m/s is plenty at kiosk distance. */
export function formatWind(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return String(Math.round(value));
}

/** Clock label for the radar frame currently shown, e.g. "14:20 Uhr". */
export function formatFrameTime(ms) {
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  return `${pad2(moment.getHours())}:${pad2(moment.getMinutes())} Uhr`;
}
