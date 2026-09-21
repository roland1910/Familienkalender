// Pure groundwater-view formatting (no DOM), unit-testable with node
// --test. German display strings, local time — like the rest of the
// frontend. Everything returns "" (or a neutral fallback) for a value the
// backend could not read, so the view renders a gap instead of "undefined".

function pad2(number) {
  return String(number).padStart(2, "0");
}

function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * The water table above sea level, e.g. 508.98 → "508,98 m ü. NN".
 * This is the figure the curves plot.
 */
export function formatLevel(value) {
  if (!isNumber(value)) return "";
  return `${value.toLocaleString("de-DE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} m ü. NN`;
}

/**
 * The depth below ground (Flurabstand), e.g. 4.28 → "4,28 m unter Gelände".
 * Deliberately NOT drawn next to the level curve: it is the same
 * measurement from the other end, runs the opposite way and is two orders
 * of magnitude smaller (see groundwater-chart.js).
 */
export function formatDepth(value) {
  if (!isNumber(value)) return "";
  return `${value.toLocaleString("de-DE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} m unter Gelände`;
}

/** Axis label of the detail chart, e.g. 508.25 → "508,25". */
export function formatAxisLevel(value) {
  if (!isNumber(value)) return "";
  return value.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** When a reading was taken, e.g. "19.09.2026, 10:00 Uhr" (local time). */
export function formatMeasuredAt(ms) {
  if (!isNumber(ms)) return "";
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  const date = `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}.${moment.getFullYear()}`;
  return `${date}, ${pad2(moment.getHours())}:${pad2(moment.getMinutes())} Uhr`;
}

/** Short date for the detail chart's time axis, e.g. "19.09.". */
export function formatAxisDay(ms) {
  if (!isNumber(ms)) return "";
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  return `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}.`;
}

/** Which aquifer storey a station taps. */
export function tierLabel(tier) {
  if (tier === "upper") return "oberes Stockwerk";
  if (tier === "deep") return "tieferes Stockwerk";
  return "";
}

// The NID's low-water classes (app/groundwater.py: SITUATION_CLASSES).
// 0 = no low water … 3 = a new record low. A station the NID does not rate
// carries no class at all, which is the absence of a rating, not a fourth
// class — hence the separate neutral entry.
export const SITUATION_LABELS = {
  0: "kein Niedrigwasser",
  1: "niedrig",
  2: "sehr niedrig",
  3: "neuer Niedrigstwert",
};
export const NO_SITUATION_LABEL = "keine Einstufung";

// Theme variables per class, so the colours follow light/dark (no hard
// coded colour anywhere in the view). Classes 2 and 3 are both red — the
// palette has no fifth hue — so the view gives class 3 a second, non-colour
// cue as well (a larger dot), which also helps a colour-blind reader.
export const SITUATION_COLOR_VARS = {
  0: "--ok-border",
  1: "--warn-text",
  2: "--alert-border",
  3: "--alert-text",
};
export const NO_SITUATION_COLOR_VAR = "--text-muted";

function isClass(value) {
  return Number.isInteger(value) && value >= 0 && value <= 3;
}

/**
 * The low-water rating in plain German. The NID's own wording wins when it
 * is there (it is a foreign string and is only ever rendered via
 * textContent); the class table is the fallback, and an unrated station
 * says so instead of showing nothing.
 */
export function situationLabel(situation, situationClass) {
  if (typeof situation === "string" && situation.trim() !== "") return situation.trim();
  if (isClass(situationClass)) return SITUATION_LABELS[situationClass];
  return NO_SITUATION_LABEL;
}

/** The CSS custom property holding a class's colour. */
export function situationColorVar(situationClass) {
  return isClass(situationClass) ? SITUATION_COLOR_VARS[situationClass] : NO_SITUATION_COLOR_VAR;
}

/** Legend rows: every class plus the "not rated" entry, in order. */
export function legendEntries() {
  const entries = [0, 1, 2, 3].map((situationClass) => ({
    situationClass,
    label: SITUATION_LABELS[situationClass],
    colorVar: SITUATION_COLOR_VARS[situationClass],
  }));
  entries.push({
    situationClass: null,
    label: NO_SITUATION_LABEL,
    colorVar: NO_SITUATION_COLOR_VAR,
  });
  return entries;
}

/** Summary line under the map, e.g. "164 Messstellen im Umkreis von 50 km". */
export function stationCountLabel(count) {
  if (!Number.isInteger(count) || count < 0) return "";
  const noun = count === 1 ? "Messstelle" : "Messstellen";
  return `${count} ${noun} im Umkreis von 50 km`;
}
