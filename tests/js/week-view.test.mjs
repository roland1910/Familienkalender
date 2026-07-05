// Unit tests for the collapsed night hours of the week view: the time
// grid starts at 08:00 unless a timed event is rendered earlier in the
// visible week — then at that event's full hour. All-day events and
// events outside the week never expand the grid.

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseEvent } from "../../app/static/js/events.js";
import { gridStartHour } from "../../app/static/js/week-view.js";

// Monday 2026-07-06; date-times without offset are parsed as local time,
// matching what the views do with the API payload.
const WEEK_START = new Date(2026, 6, 6);

function timed(startISO, endISO) {
  return parseEvent({
    source_id: 1,
    uid: "t",
    title: "Termin",
    start: startISO,
    end: endISO,
    all_day: false,
  });
}

function allDay(startISO, endISO) {
  return parseEvent({
    source_id: 1,
    uid: "a",
    title: "Ganztägig",
    start: startISO,
    end: endISO,
    all_day: true,
  });
}

test("gridStartHour: no events -> default 8", () => {
  assert.equal(gridStartHour([], WEEK_START), 8);
});

test("gridStartHour: only later events -> stays at 8", () => {
  const events = [timed("2026-07-07T10:00:00", "2026-07-07T11:00:00")];
  assert.equal(gridStartHour(events, WEEK_START), 8);
});

test("gridStartHour: a 06:30 event starts the grid at 06:00", () => {
  const events = [timed("2026-07-08T06:30:00", "2026-07-08T07:30:00")];
  assert.equal(gridStartHour(events, WEEK_START), 6);
});

test("gridStartHour: a 00:15 event starts the grid at 00:00", () => {
  const events = [timed("2026-07-06T00:15:00", "2026-07-06T01:00:00")];
  assert.equal(gridStartHour(events, WEEK_START), 0);
});

test("gridStartHour: the earliest of several events wins", () => {
  const events = [
    timed("2026-07-07T07:45:00", "2026-07-07T08:30:00"),
    timed("2026-07-09T05:00:00", "2026-07-09T06:00:00"),
  ];
  assert.equal(gridStartHour(events, WEEK_START), 5);
});

test("gridStartHour: all-day events never expand the grid", () => {
  // All-day events live in the bar section above the grid, not on it.
  const events = [allDay("2026-07-06", "2026-07-09")];
  assert.equal(gridStartHour(events, WEEK_START), 8);
});

test("gridStartHour: events in another week are ignored", () => {
  const events = [timed("2026-07-13T06:00:00", "2026-07-13T07:00:00")];
  assert.equal(gridStartHour(events, WEEK_START), 8);
});

test("gridStartHour: a multi-day timed event spanning full days is a bar, not grid content", () => {
  // Spans several calendar days -> rendered as an all-day bar (see
  // spansFullDays), so it must not expand the time grid either.
  const events = [timed("2026-07-04T09:00:00", "2026-07-07T17:00:00")];
  assert.equal(gridStartHour(events, WEEK_START), 8);
});
