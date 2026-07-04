// Central mutable UI state. Rendering is orchestrated by main.js; the
// views are pure functions of (anchor, events, today).

export const state = {
  mode: "calendar", // "calendar" | "power" — which main view is on screen
  view: "month", // "month" | "week"
  anchor: new Date(), // any day inside the visible period
  events: [], // parsed events (see events.js)
  tags: {}, // day tags: ISO date -> emoji list (from /api/tags)
  lastRawEvents: [], // raw /api/events payload behind state.events (partial-failure fallback)
  lastRawTags: {}, // raw /api/tags payload behind state.tags (partial-failure fallback)
  tagOptions: [], // fixed symbol catalog for the picker (from /api/tags/options)
  maxTagsPerDay: 3, // server-side cap, delivered with the catalog
  fingerprint: "", // range + raw payload; skips re-render when unchanged
  loaded: false, // first successful fetch done
  stale: false, // last fetch failed → "Daten nicht aktuell"
};
