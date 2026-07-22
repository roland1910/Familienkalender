// Pure weather-view formatting (no DOM), unit-testable with node --test.
// German display strings, local time — like the rest of the frontend.

function pad2(number) {
  return String(number).padStart(2, "0");
}

const WEEKDAYS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

/**
 * X-axis tick label for a round local hour, e.g. "06:00" (Etappe 38). The
 * day a tick belongs to is written above the chart by `formatDayLabel`, so
 * the tick itself only carries the clock — no more crooked "Do 03:12".
 */
export function formatHourTick(ms) {
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  return `${pad2(moment.getHours())}:${pad2(moment.getMinutes())}`;
}

/** Label of a day column above the chart, e.g. "Mi 22.07.". */
export function formatDayLabel(ms) {
  const parts = formatDayParts(ms);
  return parts === null ? "" : `${parts.weekday} ${parts.date}`;
}

/**
 * The same label split into its two halves: `{weekday, date}`, e.g.
 * `{weekday: "Mi", date: "22.07."}`.
 *
 * Etappe 39 — Roland: "besonders die Wochetage mit Datum sehen komisch aus".
 * The chart draws the weekday semibold and the date behind it in a lighter
 * tone, so the eye gets the day at a glance and the date only when it looks
 * for it. Null for an unreadable timestamp (the caller then draws nothing).
 */
export function formatDayParts(ms) {
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return null;
  const day = pad2(moment.getDate());
  const month = pad2(moment.getMonth() + 1);
  return { weekday: WEEKDAYS[moment.getDay()], date: `${day}.${month}.` };
}

/** Temperature axis label, e.g. 12.5 → "13°". */
export function formatTemp(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return `${Math.round(value)}°`;
}

/**
 * Precipitation axis label, e.g. 1.5 → "1,5 mm" (German decimal comma).
 * Up to two decimals: with a 1 mm axis the gridlines land on quarters,
 * and rounding 0.25 to "0,3 mm" would misstate the line it belongs to.
 * Trailing zeros are dropped, so a whole number stays "1 mm".
 */
export function formatPrecip(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return `${value.toLocaleString("de-DE", { maximumFractionDigits: 2 })} mm`;
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
