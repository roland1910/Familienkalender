// Pure power-view logic (no DOM): value formatting and the balance-tile
// classification. Extracted out of power-view.js so it is unit-testable
// with plain node --test.

/** Watt value as a German display string, e.g. 1234.5 → "1.235 W". */
export function formatWatts(value) {
  return `${Math.round(value).toLocaleString("de-DE")} W`;
}

function pad2(number) {
  return String(number).padStart(2, "0");
}

/**
 * "As of" timestamp for a power value, from the HA state's last_updated
 * (the moment the state was last written -- the freshness of the reading;
 * last_changed would only track value *changes* and look stale under a
 * constant load).
 *
 * `iso` is an ISO-8601 string with timezone; `now` is the reference moment
 * (injected for testability). The result is local time (Europe/Berlin on
 * the kiosk, matching the rest of the frontend which also uses getHours()):
 *   - today      -> "HH:MM"
 *   - yesterday  -> "gestern HH:MM"
 *   - older      -> "TT.MM. HH:MM"
 * Missing/unparseable input returns "" so the caller renders nothing.
 */
export function formatLastUpdated(iso, now) {
  if (!iso) return "";
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return "";
  const time = `${pad2(moment.getHours())}:${pad2(moment.getMinutes())}`;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfMoment = new Date(moment.getFullYear(), moment.getMonth(), moment.getDate());
  const dayDiff = Math.round((startOfToday - startOfMoment) / 86400000);
  if (dayDiff <= 0) return time;
  if (dayDiff === 1) return `gestern ${time}`;
  return `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}. ${time}`;
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
