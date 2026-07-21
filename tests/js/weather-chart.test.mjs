// Unit tests for the pure weather chart geometry: axis bounds, scaling,
// the temperature path (which must break over missing hours), the
// precipitation bars and the wind arrow direction.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CHART_HEIGHT,
  CHART_PADDING,
  CHART_WIDTH,
  plotArea,
  precipBars,
  precipMax,
  precipTicks,
  scaleTemp,
  scaleX,
  sliceHours,
  tempBounds,
  tempPath,
  tempTicks,
  timeBounds,
  windArrowRotation,
  windSamples,
  xTicks,
} from "../../app/static/js/weather-chart.js";

const HOUR = 3600000;
const NOW = Date.UTC(2026, 6, 21, 12, 0, 0);

function series(count, build = () => ({})) {
  return Array.from({ length: count }, (_, index) => ({
    t: NOW + index * HOUR,
    temp_c: 15 + index,
    precip_mm: 0,
    wind_ms: 3,
    wind_dir_deg: 180,
    ...build(index),
  }));
}

test("plotArea sits inside the padding", () => {
  const area = plotArea();
  assert.equal(area.x, CHART_PADDING.left);
  assert.equal(area.y, CHART_PADDING.top);
  assert.equal(area.width, CHART_WIDTH - CHART_PADDING.left - CHART_PADDING.right);
  assert.equal(area.height, CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom);
});

test("sliceHours keeps only the selected window", () => {
  const points = series(49);
  assert.equal(sliceHours(points, 48, NOW).length, 49);
  // 24h window: the current hour plus the next 24.
  assert.equal(sliceHours(points, 24, NOW).length, 25);
});

test("sliceHours drops points far in the past", () => {
  const points = [{ t: NOW - 10 * HOUR }, { t: NOW }, { t: NOW + HOUR }];
  assert.deepEqual(
    sliceHours(points, 24, NOW).map((p) => p.t),
    [NOW, NOW + HOUR],
  );
});

test("timeBounds spans the series and widens a single point", () => {
  assert.deepEqual(timeBounds(series(3)), { minT: NOW, maxT: NOW + 2 * HOUR });
  const single = timeBounds([{ t: NOW }]);
  assert.ok(single.maxT > single.minT);
});

test("timeBounds returns null for an empty series", () => {
  assert.equal(timeBounds([]), null);
});

test("tempBounds snaps to round steps that cover the series", () => {
  const bounds = tempBounds([{ temp_c: 12.4 }, { temp_c: 23.1 }]);
  assert.deepEqual(bounds, { min: 10, max: 30, step: 5 });
});

test("tempBounds handles negative temperatures", () => {
  assert.deepEqual(tempBounds([{ temp_c: -3.2 }, { temp_c: 1 }]), { min: -4, max: 4, step: 2 });
});

test("tempBounds never collapses to a zero-height axis", () => {
  const bounds = tempBounds([{ temp_c: 15 }, { temp_c: 15 }]);
  assert.ok(bounds.max > bounds.min);
});

test("tempBounds always covers every temperature in the series", () => {
  const samples = [
    [-18.3, -17.9],
    [-5, 38],
    [0.1, 0.2],
    [19.6, 20.4],
    [-30, 45],
  ];
  for (const [low, high] of samples) {
    const bounds = tempBounds([{ temp_c: low }, { temp_c: high }]);
    assert.ok(bounds.min <= low, `min ${bounds.min} above ${low}`);
    assert.ok(bounds.max >= high, `max ${bounds.max} below ${high}`);
  }
});

test("tempBounds gridlines all land on whole multiples of the step", () => {
  const area = plotArea();
  for (const [low, high] of [
    [12.4, 23.1],
    [-3.2, 1],
    [-5, 38],
  ]) {
    const bounds = tempBounds([{ temp_c: low }, { temp_c: high }]);
    for (const tick of tempTicks(bounds, area)) {
      // Math.abs: a negative multiple yields -0, which strict-equals fails on.
      assert.equal(
        Math.abs(tick.value % bounds.step),
        0,
        `${tick.value} is not a multiple of ${bounds.step}`,
      );
      assert.equal(tick.value, Math.round(tick.value), "tick value is not whole");
    }
  }
});

test("tempBounds is null when no hour has a temperature", () => {
  assert.equal(tempBounds([{ temp_c: null }, {}]), null);
});

test("precipMax is at least 1 mm and rounds up to a nice number", () => {
  assert.equal(precipMax([{ precip_mm: 0 }, { precip_mm: 0 }]), 1);
  assert.equal(precipMax([{ precip_mm: 0.3 }]), 1);
  assert.equal(precipMax([{ precip_mm: 3.4 }]), 5);
  assert.equal(precipMax([{ precip_mm: null }]), 1);
});

test("scaleX maps the range onto the plot area", () => {
  const area = plotArea();
  const bounds = { minT: NOW, maxT: NOW + 10 * HOUR };
  assert.equal(scaleX(NOW, bounds, area), area.x);
  assert.equal(scaleX(NOW + 10 * HOUR, bounds, area), area.x + area.width);
});

test("scaleTemp is inverted (max at the top)", () => {
  const area = plotArea();
  const bounds = { min: 0, max: 20 };
  assert.equal(scaleTemp(20, bounds, area), area.y);
  assert.equal(scaleTemp(0, bounds, area), area.y + area.height);
});

test("tempPath draws one connected polyline", () => {
  const area = plotArea();
  const points = series(3);
  const path = tempPath(points, timeBounds(points), tempBounds(points), area);
  assert.match(path, /^M/);
  assert.equal((path.match(/M/g) || []).length, 1);
  assert.equal((path.match(/L/g) || []).length, 2);
});

test("tempPath breaks the line over hours without a temperature", () => {
  const area = plotArea();
  const points = series(4, (index) => (index === 1 ? { temp_c: null } : {}));
  const path = tempPath(points, timeBounds(points), tempBounds(points), area);
  // Two separate segments -> two "M" commands, never a line across the gap.
  assert.equal((path.match(/M/g) || []).length, 2);
});

test("tempPath is empty without any temperature", () => {
  const area = plotArea();
  const points = series(3, () => ({ temp_c: null }));
  assert.equal(tempPath(points, timeBounds(points), { min: 0, max: 5 }, area), "");
});

test("precipBars only covers hours with actual precipitation", () => {
  const area = plotArea();
  const points = series(4, (index) => ({ precip_mm: index === 2 ? 1.5 : 0 }));
  const bars = precipBars(points, timeBounds(points), precipMax(points), area);
  assert.equal(bars.length, 1);
  assert.equal(bars[0].mm, 1.5);
});

test("precipBars grow downward from the value to the baseline", () => {
  const area = plotArea();
  const points = series(2, (index) => ({ precip_mm: index === 0 ? 2 : 0 }));
  const [bar] = precipBars(points, timeBounds(points), 2, area);
  assert.equal(Math.round(bar.height), area.height);
  assert.equal(Math.round(bar.y), area.y);
  assert.ok(bar.width > 0);
});

test("precipBars skips null precipitation", () => {
  const area = plotArea();
  const points = series(3, () => ({ precip_mm: null }));
  assert.deepEqual(precipBars(points, timeBounds(points), 1, area), []);
});

test("xTicks are evenly spaced and span the range", () => {
  const area = plotArea();
  const bounds = { minT: NOW, maxT: NOW + 24 * HOUR };
  const ticks = xTicks(bounds, area, 5);
  assert.equal(ticks.length, 5);
  assert.equal(ticks[0].t, bounds.minT);
  assert.equal(ticks[4].t, bounds.maxT);
  assert.equal(Math.round(ticks[4].x), Math.round(area.x + area.width));
});

test("tempTicks include both ends of the axis", () => {
  const area = plotArea();
  const ticks = tempTicks({ min: 10, max: 30 }, area, 4);
  assert.equal(ticks.length, 5);
  assert.equal(ticks[0].value, 10);
  assert.equal(ticks[4].value, 30);
});

test("precipTicks run from zero to the ceiling", () => {
  const area = plotArea();
  const ticks = precipTicks(5, area, 5);
  assert.equal(ticks[0].value, 0);
  assert.equal(ticks[5].value, 5);
  assert.equal(Math.round(ticks[0].y), Math.round(area.y + area.height));
});

test("windArrowRotation turns the from-direction into a blows-to arrow", () => {
  // Wind FROM the south (180) blows TO the north -> arrow points up (0).
  assert.equal(windArrowRotation(180), 0);
  assert.equal(windArrowRotation(0), 180);
  assert.equal(windArrowRotation(270), 90);
  assert.equal(windArrowRotation(90), 270);
});

test("windArrowRotation normalises out-of-range and rejects non-numbers", () => {
  assert.equal(windArrowRotation(540), 0);
  assert.equal(windArrowRotation(-90), 90);
  assert.equal(windArrowRotation(null), null);
  assert.equal(windArrowRotation(undefined), null);
  assert.equal(windArrowRotation(Number.NaN), null);
  assert.equal(windArrowRotation("180"), null);
});

test("windSamples thins the series out to a readable number of arrows", () => {
  const samples = windSamples(series(48), 8);
  assert.equal(samples.length, 8);
  const times = samples.map((point) => point.t);
  assert.deepEqual(times, [...times].sort((a, b) => a - b));
});

test("windSamples passes short series through untouched", () => {
  const points = series(5);
  assert.equal(windSamples(points, 8).length, 5);
});

test("windSamples ignores hours without wind data", () => {
  const points = series(4, (index) => (index < 2 ? { wind_ms: null } : {}));
  assert.equal(windSamples(points, 8).length, 2);
});
