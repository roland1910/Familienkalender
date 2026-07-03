// Deterministic per-source colors. The palette is fixed for now and will
// become admin-configurable in a later milestone. All colors are dark
// enough for white chip text.

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

export function colorForSource(sourceId) {
  // `|| 0` tolerates unexpected ids (undefined, non-numeric → NaN): they
  // all map to the first palette color instead of an undefined lookup.
  const index = Math.abs(Number(sourceId) || 0) % PALETTE.length;
  return PALETTE[index];
}
