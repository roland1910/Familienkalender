// Unit tests for the pure weather display formatting (German, local time).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatAxisTime,
  formatFrameTime,
  formatPrecip,
  formatTemp,
  formatWind,
} from "../../app/static/js/weather-format.js";

// Local-time constructor so the assertions hold in any timezone.
const AFTERNOON = new Date(2026, 6, 22, 14, 20).getTime(); // Wednesday

test("formatAxisTime shows the clock in the 24h window", () => {
  assert.equal(formatAxisTime(AFTERNOON, 24), "14:20");
});

test("formatAxisTime prefixes the weekday in the 48h window", () => {
  assert.equal(formatAxisTime(AFTERNOON, 48), "Mi 14:20");
});

test("formatAxisTime returns an empty string for invalid input", () => {
  assert.equal(formatAxisTime(Number.NaN, 24), "");
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
