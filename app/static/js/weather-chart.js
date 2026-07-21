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
// Generous bottom padding: x labels, then the wind arrow row, then the
// wind speed numbers. Right padding holds the precipitation axis.
export const CHART_PADDING = { top: 20, right: 82, bottom: 88, left: 56 };

// Vertical offsets below the plot area for the three label rows.
export const X_LABEL_OFFSET = 22;
export const WIND_ARROW_OFFSET = 50;
export const WIND_LABEL_OFFSET = 78;
export const WIND_ARROW_SIZE = 13;

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
 * Rectangles for the precipitation bars, one per hour that actually has
 * precipitation (> 0). Bars are centred on their hour and sized from the
 * spacing between points, so 24h and 48h both look right.
 */
export function precipBars(points, timeB, maxMm, area) {
  const spacing = points.length > 1 ? area.width / (points.length - 1) : area.width;
  const width = Math.max(2, spacing * 0.6);
  const bars = [];
  for (const point of points) {
    if (typeof point.precip_mm !== "number" || point.precip_mm <= 0) continue;
    const height = Math.max(1, (point.precip_mm / maxMm) * area.height);
    bars.push({
      x: scaleX(point.t, timeB, area) - width / 2,
      y: area.y + area.height - height,
      width,
      height,
      mm: point.precip_mm,
    });
  }
  return bars;
}

/** Evenly spaced x-axis ticks across the time range. */
export function xTicks(bounds, area, count = 6) {
  const ticks = [];
  const steps = Math.max(1, count - 1);
  for (let index = 0; index < count; index += 1) {
    const t = bounds.minT + ((bounds.maxT - bounds.minT) * index) / steps;
    ticks.push({ t, x: scaleX(t, bounds, area) });
  }
  return ticks;
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

/** Precipitation gridline values sharing the temperature ticks' positions. */
export function precipTicks(maxMm, area, count = 4) {
  const ticks = [];
  for (let index = 0; index <= count; index += 1) {
    const value = (maxMm * index) / count;
    ticks.push({ value, y: area.y + area.height - (area.height * index) / count });
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
