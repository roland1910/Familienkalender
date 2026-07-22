// Unit tests for the pure weather display formatting (German, local time).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatDayLabel,
  formatDayParts,
  formatFrameTime,
  formatHourTick,
  formatPrecip,
  formatTemp,
  formatWind,
} from "../../app/static/js/weather-format.js";

// Local-time constructor so the assertions hold in any timezone.
const AFTERNOON = new Date(2026, 6, 22, 14, 20).getTime(); // Wednesday

test("formatHourTick shows the clock of a round hour", () => {
  assert.equal(formatHourTick(new Date(2026, 6, 22, 6, 0).getTime()), "06:00");
  assert.equal(formatHourTick(new Date(2026, 6, 22, 0, 0).getTime()), "00:00");
});

test("formatHourTick returns an empty string for invalid input", () => {
  assert.equal(formatHourTick(Number.NaN), "");
});

test("formatDayLabel names the weekday and the date", () => {
  assert.equal(formatDayLabel(AFTERNOON), "Mi 22.07.");
  assert.equal(formatDayLabel(new Date(2026, 0, 4, 3, 0).getTime()), "So 04.01.");
});

test("formatDayLabel returns an empty string for invalid input", () => {
  assert.equal(formatDayLabel(Number.NaN), "");
});

test("formatTemp rounds to whole degrees", () => {
  assert.equal(formatTemp(12.4), "12°");
  assert.equal(formatTemp(-0.6), "-1°");
  assert.equal(formatTemp(null), "");
  assert.equal(formatTemp(Number.NaN), "");
});

test("formatPrecip uses the German decimal comma", () => {
  assert.equal(formatPrecip(1.5), "1,5 mm");
  assert.equal(formatPrecip(0), "0 mm");
  assert.equal(formatPrecip(null), "");
});

test("formatPrecip keeps quarter steps exact and drops trailing zeros", () => {
  // A 1 mm axis has gridlines at 0.25/0.5/0.75 — "0,3 mm" would be wrong.
  assert.equal(formatPrecip(0.25), "0,25 mm");
  assert.equal(formatPrecip(0.75), "0,75 mm");
  assert.equal(formatPrecip(1), "1 mm");
  assert.equal(formatPrecip(2), "2 mm");
});

test("formatWind rounds to whole m/s", () => {
  assert.equal(formatWind(3.4), "3");
  assert.equal(formatWind(0.4), "0");
  assert.equal(formatWind(null), "");
});

test("formatFrameTime labels a radar frame with its clock time", () => {
  assert.equal(formatFrameTime(AFTERNOON), "14:20 Uhr");
  assert.equal(formatFrameTime(Number.NaN), "");
});

test("formatDayParts splits the label so the chart can style the two halves", () => {
  // Etappe 39: the weekday is drawn semibold, the date behind it lighter.
  assert.deepEqual(formatDayParts(AFTERNOON), { weekday: "Mi", date: "22.07." });
  assert.deepEqual(formatDayParts(new Date(2026, 0, 4, 3, 0).getTime()), {
    weekday: "So",
    date: "04.01.",
  });
});

test("formatDayParts returns null for invalid input", () => {
  assert.equal(formatDayParts(Number.NaN), null);
});
