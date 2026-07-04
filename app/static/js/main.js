// App wiring: navigation, data loading, auto-refresh, view rendering.

import { fetchEvents, fetchTagOptions, fetchTags } from "./api.js";
import {
  addDays,
  addMonths,
  formatDayMonth,
  isoWeekNumber,
  MONTH_NAMES,
  startOfDay,
  toISODate,
} from "./dates.js";
import { parseEvent } from "./events.js";
import { attachSwipe } from "./gestures.js";
import { monthGridRange, renderMonthView } from "./month-view.js";
import { closeDayPopover, initPopover } from "./popover.js";
import { state } from "./state.js";
import { renderWeekView, weekRange } from "./week-view.js";

const REFRESH_INTERVAL_MS = 60000;
// Extra days fetched around the visible range so paging feels instant.
const FETCH_BUFFER_DAYS = 7;

function today() {
  return startOfDay(new Date());
}

function visibleRange() {
  return state.view === "month" ? monthGridRange(state.anchor) : weekRange(state.anchor);
}

function periodTitle() {
  if (state.view === "month") {
    return `${MONTH_NAMES[state.anchor.getMonth()]} ${state.anchor.getFullYear()}`;
  }
  const { start, end } = weekRange(state.anchor);
  return `KW ${isoWeekNumber(start)} · ${formatDayMonth(start)} – ${formatDayMonth(end)}`;
}

function render() {
  document.getElementById("period-title").textContent = periodTitle();
  document.getElementById("btn-month").classList.toggle("active", state.view === "month");
  document.getElementById("btn-week").classList.toggle("active", state.view === "week");
  if (!state.loaded) return; // keep the loading indicator until first data
  const container = document.getElementById("calendar");
  if (state.view === "month") {
    renderMonthView(container, state.anchor, state.events, today(), state.tags);
  } else {
    renderWeekView(container, state.anchor, state.events, today(), state.tags);
  }
}

function setStale(stale) {
  state.stale = stale;
  document.getElementById("status-badge").hidden = !stale;
}

async function loadTagOptions() {
  // The symbol catalog is fixed per server version; failures are non-fatal
  // (the picker shows a hint) and refresh() retries while it is empty.
  try {
    const payload = await fetchTagOptions();
    state.tagOptions = payload.options;
    state.maxTagsPerDay = payload.max_per_day;
  } catch {
    // keep the previous (possibly empty) catalog
  }
}

async function refresh() {
  const { start, end } = visibleRange();
  const fromISO = toISODate(addDays(start, -FETCH_BUFFER_DAYS));
  const toISO = toISODate(addDays(end, FETCH_BUFFER_DAYS));
  if (state.tagOptions.length === 0) loadTagOptions();
  let rawEvents;
  let rawTags;
  try {
    [rawEvents, rawTags] = await Promise.all([
      fetchEvents(fromISO, toISO),
      fetchTags(fromISO, toISO),
    ]);
  } catch {
    // Keep showing the last known data, just flag it as possibly outdated.
    setStale(true);
    return;
  }
  setStale(false);
  // Light DOM diff: skip re-rendering entirely when nothing changed, so the
  // 60s auto-refresh never causes flicker on the kiosk display. Tags are part
  // of the fingerprint so changes made on other devices show up too.
  const fingerprint = `${fromISO}|${toISO}|${JSON.stringify(rawEvents)}|${JSON.stringify(rawTags)}`;
  if (state.loaded && fingerprint === state.fingerprint) return;
  state.fingerprint = fingerprint;
  state.events = rawEvents.map(parseEvent);
  state.tags = rawTags;
  state.loaded = true;
  render();
}

function navigate(step) {
  closeDayPopover();
  state.anchor =
    state.view === "month" ? addMonths(state.anchor, step) : addDays(state.anchor, step * 7);
  render();
  refresh();
}

function goToToday() {
  closeDayPopover();
  state.anchor = today();
  render();
  refresh();
}

function switchView(view) {
  if (state.view === view) return;
  closeDayPopover();
  state.view = view;
  render();
  refresh();
}

function init() {
  initPopover({ onTagsChanged: render });
  document.getElementById("btn-prev").addEventListener("click", () => navigate(-1));
  document.getElementById("btn-next").addEventListener("click", () => navigate(1));
  document.getElementById("btn-today").addEventListener("click", goToToday);
  document.getElementById("btn-month").addEventListener("click", () => switchView("month"));
  document.getElementById("btn-week").addEventListener("click", () => switchView("week"));
  attachSwipe(document.getElementById("calendar"), {
    onSwipeLeft: () => navigate(1),
    onSwipeRight: () => navigate(-1),
  });
  render();
  refresh();
  setInterval(refresh, REFRESH_INTERVAL_MS);
}

init();
