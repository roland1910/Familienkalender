// Unit tests for the groundwater curve geometry: the marker sparkline and
// the detail chart in the popover. Both plot metres above sea level only —
// the depth below ground never shares an axis with them (see the module
// docstring in groundwater-chart.js).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  detailArea,
  LEVEL_INTERVALS,
  levelAxis,
  levelPath,
  levelTicks,
  scaleLevel,
  scaleTime,
  sparklinePoints,
  sparklinePolyline,
  timeBounds,
  valueBounds,
} from "../../app/static/js/groundwater-chart.js";

const BOX = { width: 40, height: 18 };

function spark(values) {
  return sparklinePoints(values, BOX.width, BOX.height);
}

test("valueBounds ignores anything that is not a finite number", () => {
  assert.deepEqual(valueBounds([508.9, 509.4, 508.2]), { min: 508.2, max: 509.4 });
  assert.deepEqual(valueBounds([null, 5, "7", Number.NaN, Number.POSITIVE_INFINITY]), {
    min: 5,
    max: 5,
  });
  assert.equal(valueBounds([]), null);
  assert.equal(valueBounds(undefined), null);
});

test("a sparkline spreads its values evenly along x", () => {
  const points = spark([1, 2, 3, 4, 5]);
  assert.equal(points.length, 5);
  assert.equal(points[0].x, 0);
  assert.equal(points.at(-1).x, BOX.width);
  assert.ok(Math.abs(points[2].x - BOX.width / 2) < 1e-9);
});

test("a rising series is drawn rising (y is inverted)", () => {
  const points = spark([500, 501, 502]);
  assert.ok(points[0].y > points[1].y);
  assert.ok(points[1].y > points[2].y);
});

test("EVERY sparkline scales to its own min/max", () => {
  // The whole point of the per-curve scale: in the 50 km radius the
  // absolute levels run from ~404 m to ~660 m, so a shared axis would draw
  // all 165 curves as flat lines. Two stations 250 m apart in height but
  // with the same shape must produce the SAME sparkline.
  const flatland = spark([404.0, 404.5, 404.2, 404.8]);
  const hills = spark([654.0, 654.5, 654.2, 654.8]);
  assert.equal(flatland.length, hills.length);
  flatland.forEach((point, index) => {
    // Tolerance, not equality: the two series reach the same shape through
    // different subtractions, so the last bit of a double may differ.
    assert.ok(Math.abs(point.x - hills[index].x) < 1e-6, `x differs at ${index}`);
    assert.ok(Math.abs(point.y - hills[index].y) < 1e-6, `y differs at ${index}`);
  });

  // …and a station that barely moves still shows its own movement.
  const tiny = spark([508.95, 508.97, 508.93, 508.99]);
  assert.ok(Math.max(...tiny.map((p) => p.y)) - Math.min(...tiny.map((p) => p.y)) > 5);
});

test("a flat series is a horizontal line, not a division by zero", () => {
  const points = spark([500, 500, 500]);
  assert.ok(points.every((point) => Number.isFinite(point.y)));
  assert.equal(new Set(points.map((point) => point.y)).size, 1);
});

test("a single value is drawn as a full-width horizontal line", () => {
  const points = spark([500]);
  assert.equal(points.length, 2);
  assert.equal(points[0].x, 0);
  assert.equal(points[1].x, BOX.width);
  assert.equal(points[0].y, points[1].y);
});

test("a sparkline stays inside its box", () => {
  for (const point of spark([400, 660, 500, 404])) {
    assert.ok(point.x >= 0 && point.x <= BOX.width, `x ${point.x}`);
    assert.ok(point.y >= 0 && point.y <= BOX.height, `y ${point.y}`);
  }
});

test("an empty or broken series yields no sparkline at all", () => {
  assert.deepEqual(spark([]), []);
  assert.deepEqual(spark(undefined), []);
  assert.deepEqual(spark([null, "x", Number.NaN]), []);
  assert.equal(sparklinePolyline([], BOX.width, BOX.height), "");
  assert.equal(sparklinePolyline(undefined, BOX.width, BOX.height), "");
});

test("the polyline attribute is a plain coordinate list", () => {
  const attribute = sparklinePolyline([1, 2], BOX.width, BOX.height);
  assert.match(attribute, /^\d+\.\d,\d+\.\d \d+\.\d,\d+\.\d$/);
});

test("non-numeric values are skipped, not plotted as zero", () => {
  // A zero would be 500 m below the real curve and flatten everything else.
  const points = spark([508.9, null, 509.1]);
  assert.equal(points.length, 2);
  assert.ok(points[0].y > points[1].y);
});

// -- detail chart ------------------------------------------------------------

const PADDING = { top: 10, right: 12, bottom: 24, left: 52 };
const AREA = detailArea(600, 240, PADDING);

function history(values, startT = 1_700_000_000_000) {
  return values.map((v, index) => ({ t: startT + index * 86_400_000, v }));
}

test("the detail area sits inside the padding", () => {
  assert.deepEqual(AREA, { x: 52, y: 10, width: 600 - 52 - 12, height: 240 - 10 - 24 });
});

test("the level axis snaps to round steps around the series", () => {
  const axis = levelAxis(history([508.12, 508.93, 508.44]));
  assert.equal(axis.max - axis.min, axis.step * LEVEL_INTERVALS);
  assert.ok(axis.min <= 508.12 + 1e-9, axis.min);
  assert.ok(axis.max >= 508.93 - 1e-9, axis.max);
  // A station moving by 80 cm must not be forced onto a 1 m grid.
  assert.ok(axis.step <= 0.5, axis.step);
});

test("the level axis copes with a tiny and with a huge span", () => {
  const tiny = levelAxis(history([508.9, 508.92]));
  assert.ok(tiny.step >= 0.05, tiny.step);
  assert.ok(tiny.min <= 508.9 && tiny.max >= 508.92);

  const huge = levelAxis(history([404, 660]));
  assert.ok(huge.min <= 404 && huge.max >= 660, huge);
  assert.equal(huge.max - huge.min, huge.step * LEVEL_INTERVALS);
});

test("a flat history still produces a usable axis", () => {
  const axis = levelAxis(history([508.9, 508.9]));
  assert.ok(axis.max > axis.min, axis);
});

test("no history means no axis and no bounds (the caller shows the hint)", () => {
  assert.equal(levelAxis([]), null);
  assert.equal(levelAxis(undefined), null);
  assert.equal(levelAxis(history([])), null);
  assert.equal(timeBounds([]), null);
});

test("the curve runs left to right and high water is at the top", () => {
  const points = history([508.0, 509.0]);
  const axis = levelAxis(points);
  const bounds = timeBounds(points);
  assert.ok(scaleTime(points[0].t, bounds, AREA) < scaleTime(points[1].t, bounds, AREA));
  assert.ok(scaleLevel(509.0, axis, AREA) < scaleLevel(508.0, axis, AREA));
  // Both ends touch the edges of the plot area.
  assert.equal(scaleTime(points[0].t, bounds, AREA), AREA.x);
  assert.equal(scaleTime(points[1].t, bounds, AREA), AREA.x + AREA.width);
});

test("a single history point does not divide by zero", () => {
  const points = history([508.5]);
  const bounds = timeBounds(points);
  assert.ok(Number.isFinite(scaleTime(points[0].t, bounds, AREA)));
  assert.match(levelPath(points, bounds, levelAxis(points), AREA), /^M/);
});

test("the path breaks at a gap instead of drawing across it", () => {
  const points = [
    { t: 1, v: 508.0 },
    { t: 2, v: null },
    { t: 3, v: 509.0 },
  ];
  const path = levelPath(points, timeBounds(points), levelAxis(points), AREA);
  assert.equal(path.match(/M/g).length, 2, path);
  assert.ok(!path.includes("L"), path);
});

test("an empty history yields an empty path", () => {
  assert.equal(levelPath([], { minT: 0, maxT: 1 }, { min: 0, max: 1, step: 0.25 }, AREA), "");
  assert.equal(levelPath(undefined, { minT: 0, maxT: 1 }, { min: 0, max: 1, step: 1 }, AREA), "");
});

test("the gridlines run from the top of the axis down", () => {
  const axis = { min: 508, max: 509, step: 0.25 };
  const ticks = levelTicks(axis, AREA);
  assert.equal(ticks.length, LEVEL_INTERVALS + 1);
  assert.equal(ticks[0].value, 509);
  assert.equal(ticks.at(-1).value, 508);
  assert.ok(ticks[0].y < ticks.at(-1).y);
  assert.equal(ticks[0].y, AREA.y);
  assert.equal(ticks.at(-1).y, AREA.y + AREA.height);
});
