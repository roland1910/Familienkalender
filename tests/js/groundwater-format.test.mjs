// Unit tests for the German display strings of the groundwater view.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  NO_SITUATION_COLOR_VAR,
  NO_SITUATION_LABEL,
  SITUATION_LABELS,
  formatAxisDay,
  formatAxisLevel,
  formatDepth,
  formatLevel,
  formatMeasuredAt,
  legendEntries,
  situationColorVar,
  situationLabel,
  stationCountLabel,
  tierLabel,
} from "../../app/static/js/groundwater-format.js";

test("the water level is printed with a decimal comma and its unit", () => {
  assert.equal(formatLevel(508.98), "508,98 m ü. NN");
  assert.equal(formatLevel(404), "404,00 m ü. NN");
  assert.equal(formatLevel(660.5), "660,50 m ü. NN");
});

test("the depth below ground says what it is measured from", () => {
  // It must never be mistaken for a height above sea level.
  assert.equal(formatDepth(4.28), "4,28 m unter Gelände");
  assert.equal(formatDepth(0), "0,00 m unter Gelände");
});

test("a missing measurement renders as nothing at all", () => {
  for (const value of [null, undefined, Number.NaN, "508,98", Number.POSITIVE_INFINITY]) {
    assert.equal(formatLevel(value), "");
    assert.equal(formatDepth(value), "");
    assert.equal(formatAxisLevel(value), "");
    assert.equal(formatMeasuredAt(value), "");
    assert.equal(formatAxisDay(value), "");
  }
});

test("the axis label is the bare number", () => {
  assert.equal(formatAxisLevel(508.25), "508,25");
});

test("the measurement time is German and local", () => {
  const moment = new Date(2026, 8, 19, 10, 5);
  assert.equal(formatMeasuredAt(moment.getTime()), "19.09.2026, 10:05 Uhr");
  assert.equal(formatAxisDay(moment.getTime()), "19.09.");
});

test("the aquifer storey is spelled out", () => {
  assert.equal(tierLabel("upper"), "oberes Stockwerk");
  assert.equal(tierLabel("deep"), "tieferes Stockwerk");
  assert.equal(tierLabel("nonsense"), "");
  assert.equal(tierLabel(undefined), "");
});

test("the NID's own wording wins over the class table", () => {
  assert.equal(situationLabel("sehr niedrig", 1), "sehr niedrig");
  assert.equal(situationLabel("  niedrig  ", null), "niedrig");
});

test("a station without wording falls back to its class", () => {
  for (const [key, label] of Object.entries(SITUATION_LABELS)) {
    assert.equal(situationLabel(null, Number(key)), label);
    assert.equal(situationLabel("", Number(key)), label);
  }
});

test("an unrated station says so instead of showing nothing", () => {
  assert.equal(situationLabel(null, null), NO_SITUATION_LABEL);
  assert.equal(situationLabel(undefined, undefined), NO_SITUATION_LABEL);
  assert.equal(situationLabel(null, 7), NO_SITUATION_LABEL);
  assert.equal(situationLabel(null, 1.5), NO_SITUATION_LABEL);
});

test("every class maps to a theme variable, never a fixed colour", () => {
  for (const situationClass of [0, 1, 2, 3]) {
    assert.match(situationColorVar(situationClass), /^--[a-z-]+$/);
  }
  assert.equal(situationColorVar(null), NO_SITUATION_COLOR_VAR);
  assert.equal(situationColorVar(42), NO_SITUATION_COLOR_VAR);
  // The four classes are told apart by four different variables.
  const used = new Set([0, 1, 2, 3].map(situationColorVar));
  assert.equal(used.size, 4);
});

test("the legend lists the four classes plus the unrated case", () => {
  const entries = legendEntries();
  assert.equal(entries.length, 5);
  assert.deepEqual(
    entries.map((entry) => entry.label),
    ["kein Niedrigwasser", "niedrig", "sehr niedrig", "neuer Niedrigstwert", NO_SITUATION_LABEL],
  );
  assert.equal(entries.at(-1).situationClass, null);
  assert.ok(entries.every((entry) => entry.colorVar.startsWith("--")));
});

test("the station count reads as a German sentence", () => {
  assert.equal(stationCountLabel(164), "164 Messstellen im Umkreis von 50 km");
  assert.equal(stationCountLabel(1), "1 Messstelle im Umkreis von 50 km");
  assert.equal(stationCountLabel(0), "0 Messstellen im Umkreis von 50 km");
  assert.equal(stationCountLabel(null), "");
  assert.equal(stationCountLabel(-1), "");
});
