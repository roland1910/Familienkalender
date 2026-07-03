// Central mutable UI state. Rendering is orchestrated by main.js; the
// views are pure functions of (anchor, events, today).

export const state = {
  view: "month", // "month" | "week"
  anchor: new Date(), // any day inside the visible period
  events: [], // parsed events (see events.js)
  fingerprint: "", // range + raw payload; skips re-render when unchanged
  loaded: false, // first successful fetch done
  stale: false, // last fetch failed → "Daten nicht aktuell"
};
