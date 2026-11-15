// Tests for formatLastUpdated: the small, muted timestamp under a power
// value. Pure formatting, so node --test covers it directly.
//
// It reads "seit ...", not "Stand ...", and that wording is the point
// (15.11.2026). HA only writes a state when it CHANGES -- measured on the
// Pi, last_changed, last_updated and last_reported were identical on every
// plug. So a washing machine that is simply off keeps its 0 W reading from
// the last time it ran, and an "as of" time would call a perfectly current
// reading stale. "seit gestern 20:33" says the true and useful thing: the
// value has not moved since then.
//
// The reference `now` is passed in so tests are deterministic; dates use
// local time (the kiosk and browsers run in Europe/Berlin, matching the
// rest of the frontend which also uses getHours()/getDate()).

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatLastUpdated } from "../../app/static/js/power-format.js";

test("formatLastUpdated shows only HH:MM for a value from today", () => {
  const now = new Date(2026, 6, 6, 15, 30, 0);
  const iso = new Date(2026, 6, 6, 9, 5, 0).toISOString();
  assert.equal(formatLastUpdated(iso, now), "seit 09:05");
});

test("formatLastUpdated prefixes 'gestern' for a value from yesterday", () => {
  const now = new Date(2026, 6, 6, 0, 10, 0);
  const iso = new Date(2026, 6, 5, 23, 45, 0).toISOString();
  assert.equal(formatLastUpdated(iso, now), "seit gestern 23:45");
});

test("formatLastUpdated shows TT.MM. HH:MM for an older value", () => {
  const now = new Date(2026, 6, 6, 12, 0, 0);
  const iso = new Date(2026, 6, 1, 8, 3, 0).toISOString();
  assert.equal(formatLastUpdated(iso, now), "seit 01.07. 08:03");
});

test("formatLastUpdated returns an empty string for null/undefined/empty", () => {
  const now = new Date(2026, 6, 6, 12, 0, 0);
  assert.equal(formatLastUpdated(null, now), "");
  assert.equal(formatLastUpdated(undefined, now), "");
  assert.equal(formatLastUpdated("", now), "");
});

test("formatLastUpdated returns an empty string for an unparseable value", () => {
  const now = new Date(2026, 6, 6, 12, 0, 0);
  assert.equal(formatLastUpdated("not-a-date", now), "");
  assert.equal(formatLastUpdated("2026-13-99T99:99:99Z", now), "");
});

test("formatLastUpdated pads single-digit hours, minutes, day and month", () => {
  const now = new Date(2026, 0, 20, 12, 0, 0);
  const iso = new Date(2026, 0, 3, 7, 4, 0).toISOString();
  assert.equal(formatLastUpdated(iso, now), "seit 03.01. 07:04");
});

test("formatLastUpdated crossing into a new year still reads as older", () => {
  const now = new Date(2026, 0, 2, 9, 0, 0);
  const iso = new Date(2025, 11, 31, 22, 15, 0).toISOString();
  assert.equal(formatLastUpdated(iso, now), "seit 31.12. 22:15");
});

test("formatLastUpdated says 'seit', never an as-of time", () => {
  // The whole point: an unchanged value is current, not stale. Measured on
  // the Pi -- the washing machine's 0 W had stood since the previous
  // evening while its plug kept reporting 229.6 V minutes ago.
  const now = new Date(2026, 10, 15, 9, 50, 0);
  const iso = new Date(2026, 10, 14, 20, 33, 0).toISOString();
  const text = formatLastUpdated(iso, now);
  assert.ok(text.startsWith("seit "), text);
  assert.ok(!text.startsWith("Stand"), text);
});
