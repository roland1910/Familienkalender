// Unit tests for the pure weather chart geometry: axis bounds, scaling,
// the temperature path (which must break over missing hours), the
// precipitation bars and the wind arrow direction.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CHART_HEIGHT,
  CHART_PADDING,
  CHART_WIDTH,
  dayBoundaries,
  daySegments,
  hourTicks,
  MIN_BAR_PX,
  nightBands,
  plotArea,
  PRECIP_HEIGHT_SHARE,
  precipBars,
  precipMax,
  precipTicks,
  scaleTemp,
  scaleX,
  sliceHours,
  tempAreaPath,
  tempBounds,
  tempPath,
  tempTicks,
  tickStepHours,
  timeBounds,
  windArrowRotation,
  windSampleCount,
  windSamples,
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

test("scaleX places unevenly spaced points by real time, not by index", () => {
  // MET switches from hourly to 6-hourly after ~63h, so the 96h window has
  // uneven gaps. The x-axis must scale by timestamp: a point at 75% of the
  // time span must sit at 75% of the width, regardless of how many points
  // precede it (an index-based layout would misplace the sparse back half).
  const area = plotArea();
  const points = [
    { t: NOW }, // hour 0
    { t: NOW + 1 * HOUR }, // dense front
    { t: NOW + 2 * HOUR },
    { t: NOW + 8 * HOUR }, // sparse back (a 6h jump)
    { t: NOW + 8 * HOUR }, // duplicate end -> minT..maxT = 8h
  ];
  const bounds = timeBounds(points);
  // The 6h point sits at 8/8 = 100% (it is the max); index-based it would be
  // at 3/4 = 75%. Assert it follows the timestamp.
  assert.equal(scaleX(NOW + 8 * HOUR, bounds, area), area.x + area.width);
  // A mid point at 2h of an 8h span sits at 25% of the width, not 2/4 = 50%.
  assert.ok(
    Math.abs(scaleX(NOW + 2 * HOUR, bounds, area) - (area.x + area.width * 0.25)) < 1e-6,
  );
});

test("tempPath positions points by timestamp under uneven spacing", () => {
  const area = plotArea();
  // Three points: 0h, 1h, 5h — the last is a sparse-step sample.
  const points = [
    { t: NOW, temp_c: 10 },
    { t: NOW + 1 * HOUR, temp_c: 12 },
    { t: NOW + 5 * HOUR, temp_c: 14 },
  ];
  const bounds = timeBounds(points);
  const path = tempPath(points, bounds, tempBounds(points), area);
  const xs = [...path.matchAll(/[ML]([\d.]+)/g)].map((m) => Number(m[1]));
  // Second point is at 1/5 of the span, not 1/2 (index-based) of the width.
  assert.ok(Math.abs(xs[1] - (area.x + area.width * 0.2)) < 0.5, `${xs[1]}`);
  assert.ok(Math.abs(xs[2] - (area.x + area.width)) < 0.5, `${xs[2]}`);
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

test("precipBars fill the band at the axis ceiling, growing up from the baseline", () => {
  const area = plotArea();
  const points = series(2, (index) => ({ precip_mm: index === 0 ? 2 : 0 }));
  const [bar] = precipBars(points, timeBounds(points), 2, area);
  // Only the lower part of the plot belongs to the bars, so the temperature
  // line stays readable above even the wettest hour.
  const band = area.height * PRECIP_HEIGHT_SHARE;
  assert.equal(Math.round(bar.height), Math.round(band));
  assert.equal(Math.round(bar.y), Math.round(area.y + area.height - band));
  assert.ok(bar.width > 0);
});

test("precipBars keep a very small amount visible", () => {
  const area = plotArea();
  // 0.05 mm on a 1 mm axis is a hairline — it must still be drawn.
  const points = series(2, (index) => ({ precip_mm: index === 0 ? 0.05 : 0 }));
  const [bar] = precipBars(points, timeBounds(points), precipMax(points), area);
  assert.ok(bar.height >= MIN_BAR_PX, `${bar.height}`);
  assert.ok(bar.width >= MIN_BAR_PX, `${bar.width}`);
});

test("precipBars scale proportionally between the floor and the band", () => {
  const area = plotArea();
  const band = area.height * PRECIP_HEIGHT_SHARE;
  const points = series(2, (index) => ({ precip_mm: index === 0 ? 2.5 : 0 }));
  const [bar] = precipBars(points, timeBounds(points), 5, area);
  assert.ok(Math.abs(bar.height - band / 2) < 0.5, `${bar.height}`);
});

test("precipBars never overlap the following point", () => {
  const area = plotArea();
  // Hourly points that each carry a six-hour sum (MET reports both): the
  // bars must stop at the next point instead of smearing into one block.
  const points = series(4, () => ({ precip_mm: 1, precip_hours: 6 }));
  const bars = precipBars(points, timeBounds(points), 1, area);
  for (let index = 0; index < bars.length - 1; index += 1) {
    assert.ok(bars[index].x + bars[index].width <= bars[index + 1].x + 0.01, `${index}`);
  }
});

test("precipBars start at their hour and span the period they cover", () => {
  const area = plotArea();
  // Six-hourly point (the sparse back half of the 96h window): the bar has
  // to cover six hours, not the one hour a dense point covers.
  const points = [
    { t: NOW, precip_mm: 1, precip_hours: 1 },
    { t: NOW + 6 * HOUR, precip_mm: 1, precip_hours: 6 },
    { t: NOW + 12 * HOUR, precip_mm: 0 },
  ];
  const bounds = timeBounds(points);
  const [hourly, sixHourly] = precipBars(points, bounds, 1, area);
  assert.equal(Math.round(hourly.x), Math.round(area.x));
  const hourPx = area.width / 12;
  assert.ok(Math.abs(hourly.width - (hourPx - 1)) < 0.5, `${hourly.width}`);
  assert.ok(Math.abs(sixHourly.width - (6 * hourPx - 1)) < 0.5, `${sixHourly.width}`);
});

test("precipBars skips null precipitation", () => {
  const area = plotArea();
  const points = series(3, () => ({ precip_mm: null }));
  assert.deepEqual(precipBars(points, timeBounds(points), 1, area), []);
});

test("tempAreaPath closes the temperature curve down to the baseline", () => {
  const area = plotArea();
  const points = series(3);
  const path = tempAreaPath(points, timeBounds(points), tempBounds(points), area);
  const baseline = area.y + area.height;
  // Starts on the baseline, walks the curve, returns to the baseline.
  assert.match(path, new RegExp(`^M[\\d.]+ ${baseline} L`));
  assert.match(path, new RegExp(`L[\\d.]+ ${baseline} Z$`));
});

test("tempAreaPath makes one shape per uninterrupted run of hours", () => {
  const area = plotArea();
  const points = series(5, (index) => (index === 2 ? { temp_c: null } : {}));
  const path = tempAreaPath(points, timeBounds(points), tempBounds(points), area);
  // The gap must not be filled over: two separate closed shapes.
  assert.equal((path.match(/Z/g) || []).length, 2, path);
});

test("tempAreaPath is empty without temperatures", () => {
  const area = plotArea();
  const points = series(3, () => ({ temp_c: null }));
  assert.equal(tempAreaPath(points, timeBounds(points), { min: 0, max: 5 }, area), "");
});

test("tempTicks include both ends of the axis", () => {
  const area = plotArea();
  const ticks = tempTicks({ min: 10, max: 30 }, area, 4);
  assert.equal(ticks.length, 5);
  assert.equal(ticks[0].value, 10);
  assert.equal(ticks[4].value, 30);
});

test("precipTicks run from zero to the ceiling inside the bar band", () => {
  const area = plotArea();
  const ticks = precipTicks(5, area, 2);
  assert.equal(ticks[0].value, 0);
  assert.equal(ticks[2].value, 5);
  assert.equal(Math.round(ticks[0].y), Math.round(area.y + area.height));
  // The ceiling label sits at the top of the bar band, not of the plot.
  const band = area.height * PRECIP_HEIGHT_SHARE;
  assert.equal(Math.round(ticks[2].y), Math.round(area.y + area.height - band));
});

// --- day grid, hour ticks and night shading (Etappe 38) --------------------
//
// These work in LOCAL time (Europe/Berlin on the kiosk), so the tests build
// their timestamps with the local Date constructor as well.

const DAY_START = new Date(2026, 6, 21, 0, 0, 0).getTime(); // Tue 21.07.2026
const localTime = (day, hour) => new Date(2026, 6, day, hour, 0, 0).getTime();

test("dayBoundaries marks every local midnight inside the range", () => {
  const bounds = { minT: localTime(21, 14), maxT: localTime(24, 9) };
  assert.deepEqual(dayBoundaries(bounds), [localTime(22, 0), localTime(23, 0), localTime(24, 0)]);
});

test("dayBoundaries is empty within a single day", () => {
  assert.deepEqual(dayBoundaries({ minT: localTime(21, 6), maxT: localTime(21, 20) }), []);
});

test("dayBoundaries ignores a midnight exactly at the range edges", () => {
  // The window starting at midnight needs no separator at its own start.
  const bounds = { minT: DAY_START, maxT: localTime(22, 0) };
  assert.deepEqual(dayBoundaries(bounds), []);
});

test("daySegments cover the range day by day and are clipped to it", () => {
  const bounds = { minT: localTime(21, 14), maxT: localTime(23, 9) };
  const segments = daySegments(bounds);
  assert.deepEqual(
    segments.map((segment) => segment.dayStart),
    [DAY_START, localTime(22, 0), localTime(23, 0)],
  );
  // First and last day are partial, the middle one is whole.
  assert.equal(segments[0].start, bounds.minT);
  assert.equal(segments[0].end, localTime(22, 0));
  assert.equal(segments[2].end, bounds.maxT);
  assert.equal(segments[1].mid, localTime(22, 12));
});

test("daySegments works when the window is inside one day", () => {
  const bounds = { minT: localTime(21, 8), maxT: localTime(21, 20) };
  const segments = daySegments(bounds);
  assert.equal(segments.length, 1);
  assert.equal(segments[0].mid, localTime(21, 14));
});

test("tickStepHours thins the axis out as the window grows", () => {
  assert.equal(tickStepHours(24), 3);
  assert.equal(tickStepHours(48), 6);
  assert.equal(tickStepHours(96), 12);
});

test("hourTicks land on round local hours, never on raw point times", () => {
  const bounds = { minT: localTime(21, 13) + 12 * 60000, maxT: localTime(22, 7) };
  const ticks = hourTicks(bounds, 6);
  assert.deepEqual(ticks, [localTime(21, 18), localTime(22, 0), localTime(22, 6)]);
  for (const tick of ticks) {
    const moment = new Date(tick);
    assert.equal(moment.getMinutes(), 0);
    assert.equal(moment.getHours() % 6, 0);
  }
});

test("hourTicks covers a 96h window without crowding it", () => {
  const bounds = { minT: localTime(21, 12), maxT: localTime(25, 12) };
  const ticks = hourTicks(bounds, tickStepHours(96));
  // 12h steps over four days: nine labels, all at 00:00 or 12:00.
  assert.equal(ticks.length, 9);
  assert.ok(ticks.every((tick) => new Date(tick).getHours() % 12 === 0));
  assert.deepEqual(ticks, [...ticks].sort((a, b) => a - b));
});

test("nightBands shade 22:00 to 06:00, clipped to the window", () => {
  const bounds = { minT: localTime(21, 18), maxT: localTime(23, 3) };
  assert.deepEqual(nightBands(bounds), [
    { start: localTime(21, 22), end: localTime(22, 6) },
    { start: localTime(22, 22), end: bounds.maxT },
  ]);
});

test("nightBands covers a window that starts in the middle of a night", () => {
  const bounds = { minT: localTime(21, 2), maxT: localTime(21, 12) };
  assert.deepEqual(nightBands(bounds), [{ start: bounds.minT, end: localTime(21, 6) }]);
});

test("nightBands is empty for a daytime-only window", () => {
  assert.deepEqual(nightBands({ minT: localTime(21, 8), maxT: localTime(21, 17) }), []);
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

test("windSampleCount drops with the width of the window", () => {
  assert.equal(windSampleCount(24), 9);
  assert.equal(windSampleCount(48), 8);
  assert.equal(windSampleCount(96), 6);
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
