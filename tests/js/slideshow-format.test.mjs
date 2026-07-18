// Tests for the pure slideshow overlay formatting helpers (taken-at date
// top right, folder trail top left).

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatFolderTrail, formatTakenAt } from "../../app/static/js/slideshow-format.js";

test("formatTakenAt renders a full date and time", () => {
  assert.equal(
    formatTakenAt({ year: 2019, month: 8, day: 16, hour: 17, minute: 30 }),
    "16.08.2019 17:30",
  );
});

test("formatTakenAt zero-pads day, month, hour and minute", () => {
  assert.equal(
    formatTakenAt({ year: 2023, month: 1, day: 2, hour: 7, minute: 8 }),
    "02.01.2023 07:08",
  );
});

test("formatTakenAt renders a date without time", () => {
  assert.equal(
    formatTakenAt({ year: 2018, month: 9, day: 23, hour: null, minute: null }),
    "23.09.2018",
  );
});

test("formatTakenAt renders a bare year", () => {
  assert.equal(formatTakenAt({ year: 2015, month: null, day: null, hour: null, minute: null }), "2015");
});

test("formatTakenAt returns empty for null/undefined/garbage", () => {
  assert.equal(formatTakenAt(null), "");
  assert.equal(formatTakenAt(undefined), "");
  assert.equal(formatTakenAt({}), "");
  assert.equal(formatTakenAt({ year: "böse" }), "");
});

test("formatTakenAt drops a time when only the hour is present", () => {
  assert.equal(formatTakenAt({ year: 2019, month: 8, day: 16, hour: 17, minute: null }), "16.08.2019");
});

test("formatTakenAt falls back to the year when the day is missing", () => {
  assert.equal(formatTakenAt({ year: 2019, month: 8, day: null, hour: null, minute: null }), "2019");
});

test("formatFolderTrail joins segments with a chevron", () => {
  assert.equal(formatFolderTrail(["Photos", "2019", "Urlaub"]), "Photos › 2019 › Urlaub");
});

test("formatFolderTrail renders a single segment as-is", () => {
  assert.equal(formatFolderTrail(["Familie"]), "Familie");
});

test("formatFolderTrail returns empty for empty/missing/garbage input", () => {
  assert.equal(formatFolderTrail([]), "");
  assert.equal(formatFolderTrail(null), "");
  assert.equal(formatFolderTrail(undefined), "");
  assert.equal(formatFolderTrail("Photos"), "");
});

test("formatFolderTrail skips non-string and empty segments", () => {
  assert.equal(formatFolderTrail(["Photos", "", null, 42, "Urlaub"]), "Photos › Urlaub");
});
