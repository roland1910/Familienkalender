// Pure power-view logic (no DOM): value formatting and the balance-tile
// classification. Extracted out of power-view.js so it is unit-testable
// with plain node --test.

/** Watt value as a German display string, e.g. 1234.5 → "1.235 W". */
export function formatWatts(value) {
  return `${Math.round(value).toLocaleString("de-DE")} W`;
}

/**
 * What the balance tile shows, mirroring the HA dashboard logic:
 * a green surplus while the PV covers the load, a red grid import
 * while power is drawn, neutral when they cancel out exactly.
 *
 * `surplus` and `gridImport` are `{value, available}` metrics from
 * /api/power. With both sensors unavailable the tile stays neutral
 * and carries the unavailable marker.
 */
export function balanceTile(surplus, gridImport) {
  if (surplus.available && surplus.value > 0) {
    return { state: "surplus", label: "Überschuss", value: surplus.value, available: true };
  }
  if (gridImport.available && gridImport.value > 0) {
    return { state: "grid", label: "Netzbezug", value: gridImport.value, available: true };
  }
  if (!surplus.available && !gridImport.available) {
    return { state: "balanced", label: "Bilanz", value: 0, available: false };
  }
  return { state: "balanced", label: "Ausgeglichen", value: 0, available: true };
}
