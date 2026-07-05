// Unit tests for the collapsed night hours of the week view (the time
// grid starts at 08:00 unless a timed event is rendered earlier in the
// visible week — then at that event's full hour; all-day events and
// events outside the week never expand the grid) and for the auto-zoom
// hour height (visible hours must fill the available grid height).

import assert from "node:assert/strict";
import { test } from "node:test";

import { parseEvent } from "../../app/static/js/events.js";
import {
  applyWeekAutoZoom,
  computeHourHeight,
  DEFAULT_HOUR_HEIGHT_PX,
  gridStartHour,
  MIN_HOUR_HEIGHT_PX,
} from "../../app/static/js/week-view.js";

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

// -- auto-zoom: hour height from the available grid height ----------------

test("computeHourHeight: visible hours exactly fill the available height", () => {
  assert.equal(computeHourHeight(960, 16), 60);
});

test("computeHourHeight: fractional heights are floored (no scrollbar from rounding)", () => {
  // 1000 / 16 = 62.5 -> 62, so 16 * 62 = 992 <= 1000 stays scroll-free.
  assert.equal(computeHourHeight(1000, 16), 62);
});

test("computeHourHeight: a large display gets tall hours (no upper cap)", () => {
  assert.equal(computeHourHeight(1920, 16), 120);
});

test("computeHourHeight: never below the readability minimum", () => {
  assert.equal(computeHourHeight(200, 16), MIN_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(1, 24), MIN_HOUR_HEIGHT_PX);
});

test("computeHourHeight: unusable heights fall back to the default", () => {
  // 0 = container hidden or not laid out yet; the default keeps the grid usable.
  assert.equal(computeHourHeight(0, 16), DEFAULT_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(-50, 16), DEFAULT_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(Number.NaN, 16), DEFAULT_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(undefined, 16), DEFAULT_HOUR_HEIGHT_PX);
});

test("computeHourHeight: unusable hour counts fall back to the default", () => {
  assert.equal(computeHourHeight(960, 0), DEFAULT_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(960, Number.NaN), DEFAULT_HOUR_HEIGHT_PX);
  assert.equal(computeHourHeight(960, -3), DEFAULT_HOUR_HEIGHT_PX);
});

// -- applyWeekAutoZoom: defensive DOM guards -------------------------------

// Minimal fakes standing in for the real DOM nodes: just enough surface
// (querySelector/dataset/style.setProperty/clientHeight) for
// applyWeekAutoZoom to run without a browser (no jsdom dependency here).
function fakeView({ withScroll, visibleHours = "16", clientHeight = 960 }) {
  const style = { properties: {}, setProperty(name, value) { this.properties[name] = value; } };
  const scroll = withScroll ? { clientHeight } : null;
  return {
    dataset: { visibleHours },
    style,
    querySelector: (selector) => (selector === ".week-scroll" ? scroll : null),
  };
}

test("applyWeekAutoZoom: no .week-view -> no-op", () => {
  const container = { querySelector: () => null };
  assert.doesNotThrow(() => applyWeekAutoZoom(container));
});

test("applyWeekAutoZoom: .week-view without .week-scroll -> no-op, no TypeError", () => {
  const view = fakeView({ withScroll: false });
  const container = { querySelector: () => view };
  assert.doesNotThrow(() => applyWeekAutoZoom(container));
  assert.deepEqual(view.style.properties, {});
});

test("applyWeekAutoZoom: both present -> sets --hour-height from the measured scroll", () => {
  const view = fakeView({ withScroll: true, visibleHours: "16", clientHeight: 960 });
  const container = { querySelector: () => view };
  applyWeekAutoZoom(container);
  assert.equal(view.style.properties["--hour-height"], "60px");
});
