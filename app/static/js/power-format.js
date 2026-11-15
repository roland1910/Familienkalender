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
 * How long a power value has been standing, from the HA state's
 * last_updated -- rendered as "seit ...", NOT as an as-of time.
 *
 * THE WORDING IS THE POINT (15.11.2026). An earlier comment here claimed
 * last_updated was "the moment the state was last written", unlike
 * last_changed which "only tracks value changes and looks stale under a
 * constant load". That is wrong, and it made working tiles look dead:
 * Home Assistant writes a state only when it actually CHANGES. Measured on
 * the Pi across every plug, last_changed, last_updated and last_reported
 * held the exact same instant. A washing machine that is merely switched
 * off therefore keeps the 0 W it last reported -- Roland saw "gestern
 * 20:33" under a reading that was perfectly current, while the same plug's
 * voltage sensor had reported 229.6 V minutes earlier.
 *
 * There is no field that would tell us the true freshness, so the honest
 * line is the one we can actually back up: this value has not moved since
 * then. That also happens to be the more useful statement -- "0 W seit
 * gestern 20:33" says the machine has been off since yesterday evening.
 *
 * `iso` is an ISO-8601 string with timezone; `now` is the reference moment
 * (injected for testability). The result is local time (Europe/Berlin on
 * the kiosk, matching the rest of the frontend which also uses getHours()):
 *   - today      -> "seit HH:MM"
 *   - yesterday  -> "seit gestern HH:MM"
 *   - older      -> "seit TT.MM. HH:MM"
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
  if (dayDiff <= 0) return `seit ${time}`;
  if (dayDiff === 1) return `seit gestern ${time}`;
  return `seit ${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}. ${time}`;
}

/**
 * X-axis tick label for a chart timestamp (epoch ms), in local time.
 * Windows of one day show "HH:MM"; longer windows show "TT.MM." so the
 * days are distinguishable. `hours` is the selected window (24/72/168).
 */
export function formatChartTime(ms, hours) {
  const moment = new Date(ms);
  if (Number.isNaN(moment.getTime())) return "";
  if (hours <= 24) {
    return `${pad2(moment.getHours())}:${pad2(moment.getMinutes())}`;
  }
  return `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}.`;
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
