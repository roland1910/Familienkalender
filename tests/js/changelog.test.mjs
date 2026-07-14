// Node unit tests for the pure change-log formatters (no DOM).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  actionClass,
  actionLabel,
  directionLabel,
  formatEntryTime,
  formatEventDate,
} from "../../app/static/admin/changelog.js";

test("direction labels are German", () => {
  assert.equal(directionLabel("in"), "eingehend");
  assert.equal(directionLabel("out"), "ausgehend");
});

test("action labels are German", () => {
  assert.equal(actionLabel("added"), "hinzugefügt");
  assert.equal(actionLabel("updated"), "geändert");
  assert.equal(actionLabel("removed"), "entfernt");
});

test("action classes map to the theme accent classes", () => {
  assert.equal(actionClass("added"), "changelog-added");
  assert.equal(actionClass("updated"), "changelog-updated");
  assert.equal(actionClass("removed"), "changelog-removed");
});

test("formatEntryTime renders local TT.MM. HH:MM", () => {
  // Built from local components and read back with local getters, so the
  // assertion is timezone-independent.
  const local = new Date(2026, 6, 14, 9, 5);
  assert.equal(formatEntryTime(local.toISOString()), "14.07. 09:05");
});

test("formatEntryTime returns empty for missing/invalid input", () => {
  assert.equal(formatEntryTime(""), "");
  assert.equal(formatEntryTime("not-a-date"), "");
});

test("formatEventDate reads all-day dates from components (no tz shift)", () => {
  assert.equal(formatEventDate("2026-07-14"), "14.07.");
});

test("formatEventDate renders a local TT.MM. for timed starts", () => {
  const local = new Date(2026, 6, 14, 16, 0);
  assert.equal(formatEventDate(local.toISOString()), "14.07.");
});

test("formatEventDate returns empty for missing/invalid input", () => {
  assert.equal(formatEventDate(""), "");
  assert.equal(formatEventDate("nope"), "");
});
