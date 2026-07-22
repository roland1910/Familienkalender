// Pure geometry for the weather forecast chart (no DOM). Extracted so it
// is unit-testable with plain node --test; the SVG itself is built in
// weather-view.js via document.createElementNS only, never HTML strings.
//
// The chart stacks three readings over one time axis:
//   - temperature as a line on the LEFT y axis (°C),
//   - precipitation as bars on the RIGHT y axis (mm/h),
//   - wind as arrows in a dedicated row below the plot (direction + m/s).
// Every input value may be null (MET Norway omits fields for some hours),
// so each helper skips nulls rather than assuming a complete series.

import { niceCeiling } from "./power-chart.js";

// SVG user-space coordinate system; the <svg> scales it via viewBox.
export const CHART_WIDTH = 1000;
export const CHART_HEIGHT = 340;
// Top padding holds the day header band (weekday + date per day, Etappe 38).
// Generous bottom padding: hour labels, then the wind arrow row, then the
// wind speed numbers. Right padding holds the precipitation axis.
export const CHART_PADDING = { top: 34, right: 82, bottom: 88, left: 56 };

// Baseline of the day labels inside the top band.
export const DAY_LABEL_Y = 22;
// Vertical offsets below the plot area for the three label rows.
export const X_LABEL_OFFSET = 22;
export const WIND_ARROW_OFFSET = 50;
export const WIND_LABEL_OFFSET = 78;
export const WIND_ARROW_SIZE = 13;

// A day narrower than this many pixels gets no label (the leading and
// trailing partial days of a window are often just a sliver).
export const MIN_DAY_LABEL_PX = 46;
// Precipitation bars only use the lower part of the plot, so a wet day does
// not bury the temperature line (the same split Yr uses).
export const PRECIP_HEIGHT_SHARE = 0.55;
// Smallest bar drawn: 0.1 mm must still be a visible mark, not nothing.
export const MIN_BAR_PX = 3;
// Local hours shaded as night.
export const NIGHT_START_HOUR = 22;
export const NIGHT_END_HOUR = 6;
const HOUR_MS = 3600000;

/** Inner plotting rectangle (inside the axis padding). */
export function plotArea(width = CHART_WIDTH, height = CHART_HEIGHT, padding = CHART_PADDING) {
  return {
    x: padding.left,
    y: padding.top,
    width: width - padding.left - padding.right,
    height: height - padding.top - padding.bottom,
  };
}

/**
 * The forecast points falling inside the selected window: from `nowMs`
 * (minus a little slack for the current hour) up to `hours` ahead. The
 * backend always delivers ~48h, so the 24h/48h switch is a pure slice.
 */
export function sliceHours(points, hours, nowMs) {
  const from = nowMs - 3600000;
  const to = nowMs + hours * 3600000;
  return points.filter((point) => point.t >= from && point.t <= to);
}

/** Time range of the points, or null when there is nothing to plot. */
export function timeBounds(points) {
  if (points.length === 0) return null;
  let minT = Number.POSITIVE_INFINITY;
  let maxT = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    if (point.t < minT) minT = point.t;
    if (point.t > maxT) maxT = point.t;
  }
  // A single point would divide by zero in scaleX; widen the range.
  if (minT === maxT) maxT = minT + 1;
  return { minT, maxT };
}

// The temperature axis always has this many intervals, and its step is
// picked from these candidates so every gridline lands on a round number
// — readable across the room, unlike 10/14/18/21/25.
export const TEMP_INTERVALS = 4;
const TEMP_STEPS = [1, 2, 5, 10, 20];

/**
 * Temperature axis bounds as ``{min, max, step}``: TEMP_INTERVALS steps of
 * a round size, covering every temperature in the series. Null when no
 * hour carries a temperature (the caller then draws no line).
 */
export function tempBounds(points) {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    if (typeof point.temp_c !== "number") continue;
    if (point.temp_c < min) min = point.temp_c;
    if (point.temp_c > max) max = point.temp_c;
  }
  if (min === Number.POSITIVE_INFINITY) return null;
  const needed = (max - min) / TEMP_INTERVALS;
  let step =
    TEMP_STEPS.find((candidate) => candidate >= needed) ?? TEMP_STEPS[TEMP_STEPS.length - 1];
  let low = Math.floor(min / step) * step;
  // Rounding the low end down can leave the fixed number of intervals
  // short of the peak; widen the step until the whole series fits.
  while (low + step * TEMP_INTERVALS < max) {
    step *= 2;
    low = Math.floor(min / step) * step;
  }
  return { min: low, max: low + step * TEMP_INTERVALS, step };
}

/**
 * Ceiling of the precipitation axis in mm, rounded to a "nice" number and
 * never below 1 mm — a dry forecast still gets a sensible axis.
 */
export function precipMax(points) {
  let max = 0;
  for (const point of points) {
    if (typeof point.precip_mm === "number" && point.precip_mm > max) max = point.precip_mm;
  }
  return Math.max(1, niceCeiling(max));
}

/** Map a timestamp (ms) to an x coordinate within the plot area. */
export function scaleX(t, bounds, area) {
  const fraction = (t - bounds.minT) / (bounds.maxT - bounds.minT);
  return area.x + fraction * area.width;
}

/** Map a temperature (°C) to a y coordinate (inverted: max at the top). */
export function scaleTemp(value, bounds, area) {
  const fraction = (value - bounds.min) / (bounds.max - bounds.min);
  return area.y + area.height - fraction * area.height;
}

/**
 * SVG path "d" for the temperature line. Hours without a temperature
 * break the line (a new "M" starts after the gap) instead of drawing a
 * straight segment across missing data.
 */
export function tempPath(points, timeB, tempB, area) {
  const commands = [];
  let penDown = false;
  for (const point of points) {
    if (typeof point.temp_c !== "number") {
      penDown = false;
      continue;
    }
    const x = scaleX(point.t, timeB, area).toFixed(1);
    const y = scaleTemp(point.temp_c, tempB, area).toFixed(1);
    commands.push(`${penDown ? "L" : "M"}${x} ${y}`);
    penDown = true;
  }
  return commands.join(" ");
}

/**
 * SVG path "d" for the area between the temperature line and the baseline.
 * Same gap handling as `tempPath`: every uninterrupted run of hours becomes
 * its own closed shape, so a hole in the data is not filled over.
 */
export function tempAreaPath(points, timeB, tempB, area) {
  const baseline = area.y + area.height;
  const parts = [];
  let run = [];
  const flush = () => {
    if (run.length >= 2) {
      const first = run[0];
      const last = run[run.length - 1];
      const line = run.map((point) => `L${point.x} ${point.y}`).join(" ");
      parts.push(`M${first.x} ${baseline} ${line} L${last.x} ${baseline} Z`);
    }
    run = [];
  };
  for (const point of points) {
    if (typeof point.temp_c !== "number") {
      flush();
      continue;
    }
    run.push({
      x: scaleX(point.t, timeB, area).toFixed(1),
      y: scaleTemp(point.temp_c, tempB, area).toFixed(1),
    });
  }
  flush();
  return parts.join(" ");
}

/**
 * Rectangles for the precipitation bars, one per point that actually has
 * precipitation (> 0).
 *
 * A bar starts at its own timestamp and spans the period the amount refers
 * to — `precip_hours` (1 normally, 6 in the sparse back half of the 96h
 * window, see app/weather.py) — but never past the next point: MET also
 * reports six-hour sums on hourly entries, and overlapping bars would smear
 * into one solid block. Height uses only `share` of the plot so the
 * temperature line stays readable above it, with a floor of MIN_BAR_PX:
 * 0.1 mm must be a visible mark rather than an invisible hairline.
 */
export function precipBars(points, timeB, maxMm, area, share = PRECIP_HEIGHT_SHARE) {
  const band = area.height * share;
  const baseline = area.y + area.height;
  const bars = [];
  points.forEach((point, index) => {
    if (typeof point.precip_mm !== "number" || point.precip_mm <= 0) return;
    const spanHours =
      typeof point.precip_hours === "number" && point.precip_hours > 0 ? point.precip_hours : 1;
    const next = points[index + 1];
    const until = Math.min(
      point.t + spanHours * HOUR_MS,
      next === undefined ? timeB.maxT : next.t,
      timeB.maxT,
    );
    const from = scaleX(point.t, timeB, area);
    const to = scaleX(until, timeB, area);
    // One pixel of air between neighbouring bars, but never below the floor.
    const width = Math.max(MIN_BAR_PX, to - from - 1);
    const height = Math.max(MIN_BAR_PX, Math.min(1, point.precip_mm / maxMm) * band);
    bars.push({ x: from, y: baseline - height, width, height, mm: point.precip_mm });
  });
  return bars;
}

/**
 * Local midnights strictly inside the range — the day separators. Stepping
 * with `setDate` keeps them on real local midnights across a DST change,
 * which adding 24h in milliseconds would not.
 */
export function dayBoundaries(bounds) {
  const marks = [];
  const cursor = new Date(bounds.minT);
  cursor.setHours(0, 0, 0, 0);
  cursor.setDate(cursor.getDate() + 1);
  while (cursor.getTime() < bounds.maxT) {
    if (cursor.getTime() > bounds.minT) marks.push(cursor.getTime());
    cursor.setDate(cursor.getDate() + 1);
  }
  return marks;
}

/**
 * One entry per local day touched by the range, clipped to it:
 * `{dayStart, start, end, mid}` (`mid` is where the day's label goes).
 * The first and last entry are usually partial days.
 */
export function daySegments(bounds) {
  const segments = [];
  const cursor = new Date(bounds.minT);
  cursor.setHours(0, 0, 0, 0);
  while (cursor.getTime() < bounds.maxT) {
    const dayStart = cursor.getTime();
    const next = new Date(dayStart);
    next.setDate(next.getDate() + 1);
    const start = Math.max(dayStart, bounds.minT);
    const end = Math.min(next.getTime(), bounds.maxT);
    if (end > start) segments.push({ dayStart, start, end, mid: (start + end) / 2 });
    cursor.setTime(next.getTime());
  }
  return segments;
}

/**
 * Spacing of the x-axis ticks in hours: round clock times, thinned out as
 * the window grows so 96 h does not turn into a wall of numbers.
 */
export function tickStepHours(hours) {
  if (hours <= 24) return 3;
  if (hours <= 48) return 6;
  return 12;
}

/**
 * Timestamps of every full local hour divisible by `stepHours` inside the
 * range — 00:00, 06:00, 12:00 … instead of the raw, crooked point times.
 */
export function hourTicks(bounds, stepHours) {
  const ticks = [];
  const cursor = new Date(bounds.minT);
  cursor.setHours(0, 0, 0, 0);
  while (cursor.getTime() < bounds.maxT) {
    for (let hour = 0; hour < 24; hour += stepHours) {
      const moment = new Date(cursor.getTime());
      moment.setHours(hour, 0, 0, 0);
      const t = moment.getTime();
      if (t >= bounds.minT && t <= bounds.maxT) ticks.push(t);
    }
    cursor.setDate(cursor.getDate() + 1);
  }
  return ticks;
}

/**
 * Night stretches (NIGHT_START_HOUR to NIGHT_END_HOUR the next morning),
 * clipped to the range — drawn as a faint shade so the days are readable at
 * a glance, like on Yr.
 */
export function nightBands(bounds) {
  const bands = [];
  const cursor = new Date(bounds.minT);
  cursor.setHours(0, 0, 0, 0);
  // Start a day early: the night the window opens in began yesterday.
  cursor.setDate(cursor.getDate() - 1);
  while (cursor.getTime() < bounds.maxT) {
    const evening = new Date(cursor.getTime());
    evening.setHours(NIGHT_START_HOUR, 0, 0, 0);
    const morning = new Date(cursor.getTime());
    morning.setDate(morning.getDate() + 1);
    morning.setHours(NIGHT_END_HOUR, 0, 0, 0);
    const start = Math.max(evening.getTime(), bounds.minT);
    const end = Math.min(morning.getTime(), bounds.maxT);
    if (end > start) bands.push({ start, end });
    cursor.setDate(cursor.getDate() + 1);
  }
  return bands;
}

/** Evenly spaced temperature gridlines from min to max (inclusive). */
export function tempTicks(bounds, area, count = TEMP_INTERVALS) {
  const ticks = [];
  for (let index = 0; index <= count; index += 1) {
    const value = bounds.min + ((bounds.max - bounds.min) * index) / count;
    ticks.push({ value, y: scaleTemp(value, bounds, area) });
  }
  return ticks;
}

/**
 * Labels of the precipitation axis, positioned inside the bar band (the
 * lower `share` of the plot) — the bars are scaled to that band, so the
 * numbers have to follow them instead of the temperature gridlines.
 */
export function precipTicks(maxMm, area, count = 2, share = PRECIP_HEIGHT_SHARE) {
  const band = area.height * share;
  const ticks = [];
  for (let index = 0; index <= count; index += 1) {
    const value = (maxMm * index) / count;
    ticks.push({ value, y: area.y + area.height - (band * index) / count });
  }
  return ticks;
}

/**
 * Rotation (degrees) for a wind arrow drawn pointing "up" at 0°.
 *
 * MET reports `wind_from_direction` — where the wind comes FROM. The
 * arrow shows where it blows TO (the convention on Yr and weather apps),
 * so it is turned by half a circle. Non-numeric input yields null and the
 * caller draws no arrow.
 */
export function windArrowRotation(fromDeg) {
  if (typeof fromDeg !== "number" || !Number.isFinite(fromDeg)) return null;
  return (((fromDeg + 180) % 360) + 360) % 360;
}

/**
 * How many wind arrows a window gets: the wider the outlook, the fewer, so
 * the arrows never crowd into a solid row.
 */
export function windSampleCount(hours) {
  if (hours <= 24) return 9;
  if (hours <= 48) return 8;
  return 6;
}

/**
 * Up to `count` evenly spaced hours to show a wind arrow for — drawing
 * one per hour would be unreadable from across the room. Only hours with
 * both a direction and a speed qualify.
 */
export function windSamples(points, count = 8) {
  const usable = points.filter(
    (point) => typeof point.wind_ms === "number" && windArrowRotation(point.wind_dir_deg) !== null,
  );
  if (usable.length <= count) return usable;
  const step = usable.length / count;
  const samples = [];
  for (let index = 0; index < count; index += 1) {
    samples.push(usable[Math.floor(index * step)]);
  }
  return samples;
}
