// Pure geometry for the power history chart (no DOM). Extracted so it is
// unit-testable with plain node --test. The SVG building itself lives in
// power-view.js (via document.createElementNS only, never HTML strings).

// SVG user-space coordinate system the chart is drawn in; the <svg> scales
// it to the container via viewBox. Chosen 2:1-ish for a wide dashboard band.
export const CHART_WIDTH = 1000;
export const CHART_HEIGHT = 360;
export const CHART_PADDING = { top: 16, right: 16, bottom: 32, left: 64 };

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
 * Data bounds across both series: x from the earliest to latest timestamp,
 * y from 0 to the max value with a little headroom. Returns null when there
 * is no plottable point at all (caller shows the empty-state hint).
 */
export function computeBounds(series) {
  let minT = Number.POSITIVE_INFINITY;
  let maxT = Number.NEGATIVE_INFINITY;
  let maxV = 0;
  let any = false;
  for (const points of series) {
    for (const point of points) {
      any = true;
      if (point.t < minT) minT = point.t;
      if (point.t > maxT) maxT = point.t;
      if (point.v > maxV) maxV = point.v;
    }
  }
  if (!any) return null;
  // A flat/degenerate x-range would divide by zero in scaleX; widen it.
  if (minT === maxT) maxT = minT + 1;
  // Headroom above the peak, and a non-zero ceiling for an all-zero series.
  const yMax = niceCeiling(maxV * 1.1) || 100;
  return { minT, maxT, yMin: 0, yMax };
}

/** Round a value up to a "nice" 1/2/5×10ⁿ number for the y-axis ceiling. */
export function niceCeiling(value) {
  if (value <= 0) return 0;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  let nice;
  if (normalized <= 1) nice = 1;
  else if (normalized <= 2) nice = 2;
  else if (normalized <= 5) nice = 5;
  else nice = 10;
  return nice * magnitude;
}

/** Map a timestamp (ms) to an x coordinate within the plot area. */
export function scaleX(t, bounds, area) {
  const fraction = (t - bounds.minT) / (bounds.maxT - bounds.minT);
  return area.x + fraction * area.width;
}

/** Map a value (W) to a y coordinate (inverted: 0 at the bottom). */
export function scaleY(v, bounds, area) {
  const fraction = (v - bounds.yMin) / (bounds.yMax - bounds.yMin);
  return area.y + area.height - fraction * area.height;
}

/**
 * SVG path "d" string for a polyline through the points. Empty string for
 * an empty series (the caller then draws nothing for it).
 */
export function linePath(points, bounds, area) {
  if (points.length === 0) return "";
  const commands = points.map((point, index) => {
    const x = scaleX(point.t, bounds, area).toFixed(1);
    const y = scaleY(point.v, bounds, area).toFixed(1);
    return `${index === 0 ? "M" : "L"}${x} ${y}`;
  });
  return commands.join(" ");
}

/**
 * Up to `count` evenly spaced y-axis ticks from 0 to yMax, each with its
 * value and pixel position. Always includes 0 and the ceiling.
 */
export function yTicks(bounds, area, count = 4) {
  const ticks = [];
  for (let index = 0; index <= count; index += 1) {
    const value = (bounds.yMax * index) / count;
    ticks.push({ value, y: scaleY(value, bounds, area) });
  }
  return ticks;
}

/**
 * Up to `count` evenly spaced x-axis ticks across the time range, each with
 * its timestamp (ms) and pixel position. Single-point ranges yield one tick.
 */
export function xTicks(bounds, area, count = 5) {
  const ticks = [];
  const steps = Math.max(1, count - 1);
  for (let index = 0; index < count; index += 1) {
    const t = bounds.minT + ((bounds.maxT - bounds.minT) * index) / steps;
    ticks.push({ t, x: scaleX(t, bounds, area) });
  }
  return ticks;
}
