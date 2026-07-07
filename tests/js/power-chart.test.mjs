// Tests for the pure power-chart geometry helpers.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  computeBounds,
  linePath,
  niceCeiling,
  plotArea,
  scaleX,
  scaleY,
  xTicks,
  yTicks,
} from "../../app/static/js/power-chart.js";

const AREA = plotArea();

test("plotArea sits inside the padding", () => {
  const area = plotArea(1000, 360, { top: 10, right: 10, bottom: 20, left: 60 });
  assert.equal(area.x, 60);
  assert.equal(area.y, 10);
  assert.equal(area.width, 930);
  assert.equal(area.height, 330);
});

test("computeBounds spans both series and starts y at 0 with headroom", () => {
  const bounds = computeBounds([
    [{ t: 100, v: 50 }, { t: 300, v: 200 }],
    [{ t: 200, v: 400 }],
  ]);
  assert.equal(bounds.minT, 100);
  assert.equal(bounds.maxT, 300);
  assert.equal(bounds.yMin, 0);
  assert.ok(bounds.yMax >= 400, "ceiling clears the peak");
});

test("computeBounds returns null for two empty series", () => {
  assert.equal(computeBounds([[], []]), null);
});

test("computeBounds widens a degenerate single-timestamp range", () => {
  const bounds = computeBounds([[{ t: 500, v: 10 }], []]);
  assert.ok(bounds.maxT > bounds.minT, "x-range is non-zero");
});

test("computeBounds gives an all-zero series a non-zero ceiling", () => {
  const bounds = computeBounds([[{ t: 1, v: 0 }, { t: 2, v: 0 }], []]);
  assert.ok(bounds.yMax > 0);
});

test("niceCeiling rounds up to 1/2/5 x 10^n", () => {
  assert.equal(niceCeiling(0), 0);
  assert.equal(niceCeiling(90), 100);
  assert.equal(niceCeiling(140), 200);
  assert.equal(niceCeiling(420), 500);
  assert.equal(niceCeiling(1100), 2000);
});

test("scaleX/scaleY map the bounds onto the plot corners", () => {
  const bounds = { minT: 0, maxT: 100, yMin: 0, yMax: 200 };
  assert.equal(scaleX(0, bounds, AREA), AREA.x);
  assert.equal(scaleX(100, bounds, AREA), AREA.x + AREA.width);
  // y is inverted: max value at the top, 0 at the bottom.
  assert.equal(scaleY(200, bounds, AREA), AREA.y);
  assert.equal(scaleY(0, bounds, AREA), AREA.y + AREA.height);
});

test("linePath builds an M...L path and is empty for no points", () => {
  const bounds = { minT: 0, maxT: 100, yMin: 0, yMax: 100 };
  assert.equal(linePath([], bounds, AREA), "");
  const d = linePath([{ t: 0, v: 0 }, { t: 100, v: 100 }], bounds, AREA);
  assert.match(d, /^M/);
  assert.ok(d.includes("L"), "second point is a lineto");
});

test("yTicks includes 0 and the ceiling", () => {
  const bounds = { minT: 0, maxT: 100, yMin: 0, yMax: 200 };
  const ticks = yTicks(bounds, AREA, 4);
  assert.equal(ticks.length, 5);
  assert.equal(ticks[0].value, 0);
  assert.equal(ticks[ticks.length - 1].value, 200);
});

test("xTicks spans the time range with the requested count", () => {
  const bounds = { minT: 1000, maxT: 5000, yMin: 0, yMax: 100 };
  const ticks = xTicks(bounds, AREA, 5);
  assert.equal(ticks.length, 5);
  assert.equal(ticks[0].t, 1000);
  assert.equal(ticks[ticks.length - 1].t, 5000);
});
