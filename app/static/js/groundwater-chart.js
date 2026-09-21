// Pure geometry for the groundwater curves (no DOM), unit-testable with
// plain node --test. Two shapes share this module:
//
//   - the MARKER SPARKLINE: a few dozen bare values from
//     /api/groundwater/sparklines, drawn ~44x22 px on the map,
//   - the DETAIL CURVE: one station's daily history from
//     /api/groundwater/history/{number}, drawn in the popover.
//
// BOTH plot the water table in metres above sea level (m ü. NN) and NOTHING
// ELSE. The depth below ground (Flurabstand) is the same measurement read
// from the other end: it runs in the opposite direction and its values are
// single-digit metres against three-digit heights. Putting the two on one
// chart would need two axes running opposite ways and would be read wrong
// every single time — the depth is shown as a number next to the curve.
//
// EVERY SPARKLINE SCALES TO ITS OWN MIN/MAX. That looks like a bug until
// you see the numbers: inside the 50 km radius the absolute levels run from
// ~404 m (Danube plain) to ~660 m (the moraine hills south of Munich),
// while one station moves by perhaps half a metre over a year. On a shared
// axis all 165 curves would be flat lines at different heights, which says
// nothing. The sparkline answers "is this station rising or falling", the
// absolute number is printed in the detail view.

// Vertical breathing room inside a sparkline box, so the extremes are not
// drawn exactly on the border.
const SPARK_PADDING_Y = 3;

/** Smallest and largest finite value of a series, or null when empty. */
export function valueBounds(values) {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const value of values ?? []) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    if (value < min) min = value;
    if (value > max) max = value;
  }
  if (min === Number.POSITIVE_INFINITY) return null;
  return { min, max };
}

/**
 * Points of a marker sparkline inside a `width` x `height` box: the values
 * spread evenly along x (they are daily readings and carry no timestamps —
 * see /api/groundwater/sparklines), scaled to their OWN min/max along y.
 *
 * A single value, or a perfectly flat series, is drawn as a horizontal line
 * through the middle rather than divided by zero.
 */
export function sparklinePoints(values, width, height) {
  const usable = (values ?? []).filter(
    (value) => typeof value === "number" && Number.isFinite(value),
  );
  const bounds = valueBounds(usable);
  if (bounds === null) return [];
  const top = SPARK_PADDING_Y;
  const bottom = Math.max(top, height - SPARK_PADDING_Y);
  const span = bounds.max - bounds.min;
  const middle = (top + bottom) / 2;
  if (usable.length === 1)
    return [
      { x: 0, y: middle },
      { x: width, y: middle },
    ];
  return usable.map((value, index) => ({
    x: (index / (usable.length - 1)) * width,
    // Inverted: a high water table is drawn at the top.
    y: span === 0 ? middle : bottom - ((value - bounds.min) / span) * (bottom - top),
  }));
}

/** The sparkline as an SVG polyline `points` attribute ("" when empty). */
export function sparklinePolyline(values, width, height) {
  return sparklinePoints(values, width, height)
    .map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`)
    .join(" ");
}

// The detail chart's axis always has this many intervals, and its step is
// picked from these candidates so every gridline lands on a round number
// (same idea as the temperature axis in weather-chart.js). The candidates
// go down to centimetres: a station can move by 20 cm in three months, and
// a 1 m step would show that as a dead straight line.
export const LEVEL_INTERVALS = 4;
const LEVEL_STEPS = [0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50];

/**
 * Axis bounds of the detail curve as `{min, max, step}` — LEVEL_INTERVALS
 * steps of a round size covering the whole series. Null when there is
 * nothing to plot (the caller then shows the German "no history" hint).
 */
export function levelAxis(points) {
  const bounds = valueBounds((points ?? []).map((point) => point?.v));
  if (bounds === null) return null;
  const needed = (bounds.max - bounds.min) / LEVEL_INTERVALS;
  let step = LEVEL_STEPS.find((candidate) => candidate >= needed) ?? LEVEL_STEPS.at(-1);
  let low = Math.floor(bounds.min / step) * step;
  // Rounding the low end down can leave the fixed number of intervals short
  // of the peak; widen the step until the whole series fits.
  while (low + step * LEVEL_INTERVALS < bounds.max) {
    step *= 2;
    low = Math.floor(bounds.min / step) * step;
  }
  return { min: low, max: low + step * LEVEL_INTERVALS, step };
}

/** Inner plotting rectangle of the detail chart. */
export function detailArea(width, height, padding) {
  return {
    x: padding.left,
    y: padding.top,
    width: Math.max(0, width - padding.left - padding.right),
    height: Math.max(0, height - padding.top - padding.bottom),
  };
}

/** Map a timestamp (ms) to an x coordinate inside the plot area. */
export function scaleTime(t, bounds, area) {
  if (bounds.maxT === bounds.minT) return area.x + area.width / 2;
  return area.x + ((t - bounds.minT) / (bounds.maxT - bounds.minT)) * area.width;
}

/** Map a level (m ü. NN) to a y coordinate (inverted: high water on top). */
export function scaleLevel(value, axis, area) {
  if (axis.max === axis.min) return area.y + area.height / 2;
  return area.y + area.height - ((value - axis.min) / (axis.max - axis.min)) * area.height;
}

/** Time range of the history points, or null when there is nothing to plot. */
export function timeBounds(points) {
  let minT = Number.POSITIVE_INFINITY;
  let maxT = Number.NEGATIVE_INFINITY;
  for (const point of points ?? []) {
    if (typeof point?.t !== "number" || !Number.isFinite(point.t)) continue;
    if (point.t < minT) minT = point.t;
    if (point.t > maxT) maxT = point.t;
  }
  if (minT === Number.POSITIVE_INFINITY) return null;
  return { minT, maxT };
}

/**
 * SVG path "d" for the detail curve. A point without a usable level breaks
 * the line (a new "M" after the gap) instead of drawing a straight segment
 * across a period nobody measured — the same rule as the temperature line.
 */
export function levelPath(points, timeB, axis, area) {
  const commands = [];
  let penDown = false;
  for (const point of points ?? []) {
    if (typeof point?.v !== "number" || !Number.isFinite(point.v)) {
      penDown = false;
      continue;
    }
    const x = scaleTime(point.t, timeB, area).toFixed(1);
    const y = scaleLevel(point.v, axis, area).toFixed(1);
    commands.push(`${penDown ? "L" : "M"}${x} ${y}`);
    penDown = true;
  }
  return commands.join(" ");
}

/** Horizontal gridlines of the detail chart: `[{value, y}]`, top to bottom. */
export function levelTicks(axis, area) {
  const ticks = [];
  for (let index = 0; index <= LEVEL_INTERVALS; index += 1) {
    const value = axis.min + axis.step * index;
    ticks.push({ value, y: scaleLevel(value, axis, area) });
  }
  return ticks.reverse();
}
