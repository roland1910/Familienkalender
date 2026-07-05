// Per-source colors: an admin-configured color (source.color, "#rrggbb")
// wins; without one a fixed palette keyed by source id applies. All
// palette colors are dark enough for white chip text.

const PALETTE = [
  "#2563eb", // blue
  "#d97706", // amber
  "#059669", // green
  "#dc2626", // red
  "#7c3aed", // violet
  "#0e7490", // cyan
  "#be185d", // pink
  "#4d7c0f", // olive
];

// Strict #rrggbb (lowercase) only. The server already validates on write,
// but the value is interpolated into a CSS custom property — defense in
// depth: anything else falls back to the palette instead of reaching CSS.
const HEX_COLOR_PATTERN = /^#[0-9a-f]{6}$/;

function paletteColor(sourceId) {
  // `|| 0` tolerates unexpected ids (undefined, non-numeric → NaN): they
  // all map to the first palette color instead of an undefined lookup.
  const index = Math.abs(Number(sourceId) || 0) % PALETTE.length;
  return PALETTE[index];
}

// Single color resolution for everything source-shaped ({id, color}) —
// chips, week bars, popover and legend must all agree on it.
export function colorForSource(source) {
  if (typeof source.color === "string" && HEX_COLOR_PATTERN.test(source.color)) {
    return source.color;
  }
  return paletteColor(source.id);
}

// Events carry their source's color as source_color (see /api/events).
export function colorForEvent(event) {
  return colorForSource({ id: event.source_id, color: event.source_color });
}
