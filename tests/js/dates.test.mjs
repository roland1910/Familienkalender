// Unit tests for the date helpers, run with the built-in node:test runner
// (npm run test:js) — no additional dependency, no build step.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addDays,
  addMonths,
  fromISODate,
  isoWeekNumber,
  startOfDay,
  startOfWeek,
  toISODate,
} from "../../app/static/js/dates.js";

// -- isoWeekNumber at year boundaries -------------------------------------

test("isoWeekNumber: 1 Jan 2027 (Friday) belongs to week 53 of 2026", () => {
  assert.equal(isoWeekNumber(new Date(2027, 0, 1)), 53);
});

test("isoWeekNumber: 31 Dec 2026 (Thursday) is week 53", () => {
  assert.equal(isoWeekNumber(new Date(2026, 11, 31)), 53);
});

test("isoWeekNumber: 1 Jan 2026 (Thursday) is week 1", () => {
  assert.equal(isoWeekNumber(new Date(2026, 0, 1)), 1);
});

test("isoWeekNumber: 29 Dec 2025 (Monday) already belongs to week 1 of 2026", () => {
  assert.equal(isoWeekNumber(new Date(2025, 11, 29)), 1);
});

test("isoWeekNumber: 30 Dec 2024 (Monday) already belongs to week 1 of 2025", () => {
  assert.equal(isoWeekNumber(new Date(2024, 11, 30)), 1);
});

// -- addMonths day-of-month overflow and year rollover ---------------------

test("addMonths: 31 Jan + 1 month anchors on 1 Feb (no overflow into March)", () => {
  assert.equal(toISODate(addMonths(new Date(2026, 0, 31), 1)), "2026-02-01");
});

test("addMonths: December + 1 month rolls over into January", () => {
  assert.equal(toISODate(addMonths(new Date(2026, 11, 15), 1)), "2027-01-01");
});

test("addMonths: January - 1 month rolls back into December", () => {
  assert.equal(toISODate(addMonths(new Date(2026, 0, 15), -1)), "2025-12-01");
});

// -- startOfWeek Monday logic around the year boundary ----------------------

test("startOfWeek: 1 Jan 2027 (Friday) belongs to the week of Mon 28 Dec 2026", () => {
  assert.equal(toISODate(startOfWeek(new Date(2027, 0, 1))), "2026-12-28");
});

test("startOfWeek: Sunday 4 Jan 2026 maps back to Mon 29 Dec 2025", () => {
  assert.equal(toISODate(startOfWeek(new Date(2026, 0, 4))), "2025-12-29");
});

test("startOfWeek: a Monday is its own week start", () => {
  assert.equal(toISODate(startOfWeek(new Date(2026, 6, 6))), "2026-07-06");
});

// -- DST days (Europe/Berlin: 29 Mar 2026 and 25 Oct 2026) ------------------
// The assertions are calendar-based, so they hold in any local timezone;
// in a DST timezone they additionally prove 23h/25h days do not shift dates.

test("addDays: crossing the spring-forward day advances exactly one date", () => {
  const next = addDays(new Date(2026, 2, 28), 1);
  assert.equal(toISODate(next), "2026-03-29");
  assert.equal(next.getHours(), 0);
  assert.equal(toISODate(addDays(new Date(2026, 2, 29), 1)), "2026-03-30");
});

test("addDays: crossing the fall-back day advances exactly one date", () => {
  const next = addDays(new Date(2026, 9, 25), 1);
  assert.equal(toISODate(next), "2026-10-26");
  assert.equal(next.getHours(), 0);
});

test("startOfDay: midnight on DST days is 00:00 local time", () => {
  for (const day of [new Date(2026, 2, 29, 15), new Date(2026, 9, 25, 15)]) {
    const start = startOfDay(day);
    assert.equal(start.getHours(), 0);
    assert.equal(start.getDate(), day.getDate());
  }
});

test("startOfWeek: a week containing a DST day still starts Monday 00:00", () => {
  const start = startOfWeek(new Date(2026, 2, 29)); // Sunday of the DST week
  assert.equal(toISODate(start), "2026-03-23");
  assert.equal(start.getHours(), 0);
});

// -- ISO date round trip -----------------------------------------------------

test("toISODate/fromISODate round-trip with zero padding", () => {
  assert.equal(toISODate(fromISODate("2026-01-05")), "2026-01-05");
  assert.equal(toISODate(new Date(2026, 0, 5)), "2026-01-05");
});
