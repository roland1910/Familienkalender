// App wiring: navigation, data loading, auto-refresh, view rendering.

import { fetchEvents, fetchMe, fetchSources, fetchTagOptions, fetchTags } from "./api.js";
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
import { renderLegend } from "./legend.js";
import { monthGridRange, renderMonthView } from "./month-view.js";
import { closeDayPopover, initPopover } from "./popover.js";
import { startPowerView, stopPowerView } from "./power-view.js";
import { loadScreensaverEnabled, saveScreensaverEnabled } from "./screensaver-memory.js";
import { isSlideshowRunning, startSlideshow, stopSlideshow } from "./slideshow-view.js";
import { state } from "./state.js";
import { loadTheme, nextTheme, saveTheme } from "./theme-memory.js";
import { loadViewState, saveViewState } from "./view-memory.js";
import { applyWeekAutoZoom, renderWeekView, weekRange } from "./week-view.js";

const REFRESH_INTERVAL_MS = 60000;
// Extra days fetched around the visible range so paging feels instant.
const FETCH_BUFFER_DAYS = 7;
// Debounce for resize/orientation events before re-fitting the week grid.
const RESIZE_DEBOUNCE_MS = 150;
// Idle time (no touch/click/key) before the screensaver slideshow starts,
// when it is enabled for this device. Overridable via a window constant so
// the E2E test does not have to wait three real minutes.
const IDLE_TIMEOUT_MS = globalThis.SCREENSAVER_IDLE_MS ?? 180000;
// How often the idle watcher checks the elapsed idle time.
const IDLE_CHECK_INTERVAL_MS = 1000;

function today() {
  return startOfDay(new Date());
}

function visibleRange() {
  return state.view === "month" ? monthGridRange(state.anchor) : weekRange(state.anchor);
}

function periodTitle() {
  if (state.mode === "power") return "Strom";
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
  // The legend belongs to the calendar views only, never to the power view.
  const legend = document.getElementById("legend");
  if (state.mode === "power") {
    legend.hidden = true;
  } else {
    renderLegend(legend, state.sources);
  }
  // The legend just changed the space above it — re-fit the week grid to
  // the final layout (first render: the legend appears after the view).
  // This deliberately re-runs applyWeekAutoZoom a second time: renderWeekView
  // already called it once (see week-view.js) against the layout without the
  // legend; when the legend is visible (calendar mode) that first result is
  // stale and gets overwritten here with the measurement that accounts for
  // the legend's height. In power mode the legend is hidden and this call is
  // a harmless no-op re-measurement of the same layout.
  if (state.view === "week") applyWeekAutoZoom(container);
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
  // Intentionally not awaited: the tag catalog is fetched independently of
  // events/tags below and must not block or fail the calendar refresh.
  if (state.tagOptions.length === 0) loadTagOptions();
  // Events, tags and sources are fetched independently (Promise.allSettled,
  // not Promise.all): if only one request fails — e.g. a flaky network
  // hiccup hits just the tags endpoint — we still want to show the data
  // that did load instead of throwing away everything. Only a complete
  // failure of the two main requests marks the data as stale (the sources
  // legend is decorative and never flags staleness on its own).
  const [eventsResult, tagsResult, sourcesResult] = await Promise.allSettled([
    fetchEvents(fromISO, toISO),
    fetchTags(fromISO, toISO),
    fetchSources(),
  ]);
  if (eventsResult.status === "rejected" && tagsResult.status === "rejected") {
    // Keep showing the last known data, just flag it as possibly outdated.
    setStale(true);
    return;
  }
  // On a partial failure, fall back to the last successfully fetched raw
  // payload for the part that failed instead of losing it entirely.
  const rawEvents = eventsResult.status === "fulfilled" ? eventsResult.value : state.lastRawEvents;
  const rawTags = tagsResult.status === "fulfilled" ? tagsResult.value : state.lastRawTags;
  const rawSources =
    sourcesResult.status === "fulfilled" ? sourcesResult.value : state.lastRawSources;
  setStale(false);
  // Light DOM diff: skip re-rendering entirely when nothing changed, so the
  // 60s auto-refresh never causes flicker on the kiosk display. Tags and
  // sources are part of the fingerprint so changes made on other devices
  // (or in the admin UI) show up too.
  const fingerprint = `${fromISO}|${toISO}|${JSON.stringify(rawEvents)}|${JSON.stringify(rawTags)}|${JSON.stringify(rawSources)}`;
  if (state.loaded && fingerprint === state.fingerprint) return;
  state.fingerprint = fingerprint;
  state.lastRawEvents = rawEvents;
  state.lastRawTags = rawTags;
  state.lastRawSources = rawSources;
  state.events = rawEvents.map(parseEvent);
  state.tags = rawTags;
  state.sources = rawSources;
  state.loaded = true;
  render();
}

// Persist the current UI position (per device via localStorage) so a
// reload — kiosk restart, browser revisit — returns to the same place.
function persistViewState() {
  saveViewState({ view: state.view, anchor: state.anchor, mode: state.mode });
}

function navigate(step) {
  closeDayPopover();
  state.anchor =
    state.view === "month" ? addMonths(state.anchor, step) : addDays(state.anchor, step * 7);
  persistViewState();
  render();
  refresh();
}

function goToToday() {
  closeDayPopover();
  // Also resets the saved anchor, so a later reload stays on today
  // instead of jumping back to a previously paged period.
  state.anchor = today();
  persistViewState();
  render();
  refresh();
}

function switchView(view) {
  if (state.view === view) return;
  closeDayPopover();
  state.view = view;
  persistViewState();
  render();
  refresh();
}

function switchMode(mode) {
  if (state.mode === mode) return;
  closeDayPopover();
  state.mode = mode;
  persistViewState();
  const isPower = mode === "power";
  // The body class hides the calendar-only toolbar controls via CSS.
  document.body.classList.toggle("mode-power", isPower);
  document.getElementById("btn-mode-calendar").classList.toggle("active", !isPower);
  document.getElementById("btn-mode-power").classList.toggle("active", isPower);
  document.getElementById("calendar").hidden = isPower;
  document.getElementById("power").hidden = !isPower;
  if (isPower) {
    startPowerView(document.getElementById("power"));
  } else {
    stopPowerView();
    refresh();
  }
  render();
}

async function applyAdminVisibility() {
  // Fail closed: the gear stays hidden unless the backend confirms admin.
  // The real gate is server-side (403 on /admin and /api/admin/*) — this
  // only removes a dead-end link for normal HA users.
  try {
    const me = await fetchMe();
    if (me.is_admin) document.querySelector(".admin-link").hidden = false;
  } catch {
    // keep the gear hidden
  }
}

// Re-fit the week grid to the new window size (debounced): only the
// --hour-height variable changes, no re-render of the events needed.
let resizeTimer;
function onWindowResized() {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    applyWeekAutoZoom(document.getElementById("calendar"));
  }, RESIZE_DEBOUNCE_MS);
}

// Restore the persisted UI position before the first render. The mode is
// deliberately included: the kiosk may live on the power view for hours,
// and a reload (watchdog, HA restart) should bring it back there instead
// of silently falling back to the calendar.
function restoreViewState() {
  const saved = loadViewState();
  if (!saved) return; // missing or invalid -> keep the defaults
  state.view = saved.view;
  state.anchor = saved.anchor;
  // view and mode are independent enums (see view-memory.js), so restoring
  // view/anchor above is a plain field assignment. mode instead goes
  // through switchMode, deliberately: it is the only place that knows how
  // to toggle the power view's DOM/lifecycle (starting startPowerView,
  // toggling body classes, hiding #calendar). This means switchMode also
  // calls persistViewState() and render() again here, writing back and
  // re-rendering the very view/anchor/mode combination that was just
  // loaded — a redundant but idempotent re-persist/render (init() calls
  // render()/refresh() again right after restoreViewState() anyway), not a
  // real user-triggered transition.
  if (saved.mode === "power") switchMode("power");
}

// -- screensaver (photo slideshow on idle) ---------------------------------
//
// Per-device toggle (localStorage). When enabled, the slideshow starts
// after IDLE_TIMEOUT_MS without any user interaction and any touch/click/
// key ends it, returning to the calendar exactly as it was. A single
// interval polls the idle time instead of resetting a timer on every input
// event (cheaper, and interaction only bumps a timestamp).

let screensaverEnabled = false;
let lastInteractionAt = Date.now();

function markInteraction() {
  lastInteractionAt = Date.now();
  // Any interaction dismisses a running slideshow immediately.
  if (isSlideshowRunning()) stopSlideshow();
}

function checkIdle() {
  if (!screensaverEnabled || isSlideshowRunning()) return;
  if (Date.now() - lastInteractionAt >= IDLE_TIMEOUT_MS) {
    startSlideshow(document.body);
  }
}

function applyScreensaverButton() {
  const button = document.getElementById("btn-screensaver");
  button.classList.toggle("active", screensaverEnabled);
  button.setAttribute("aria-pressed", String(screensaverEnabled));
}

function toggleScreensaver() {
  screensaverEnabled = !screensaverEnabled;
  saveScreensaverEnabled(screensaverEnabled);
  applyScreensaverButton();
  // Turning it off while the slideshow is up ends it right away.
  if (!screensaverEnabled) stopSlideshow();
  lastInteractionAt = Date.now();
}

function initScreensaver() {
  screensaverEnabled = loadScreensaverEnabled();
  applyScreensaverButton();
  document.getElementById("btn-screensaver").addEventListener("click", toggleScreensaver);
  // Interaction listeners bump the idle timestamp and dismiss the slideshow.
  // Capture phase so the very touch that wakes the display is counted even
  // if a child stops propagation.
  for (const type of ["pointerdown", "keydown", "wheel"]) {
    window.addEventListener(type, markInteraction, { capture: true, passive: true });
  }
  setInterval(checkIdle, IDLE_CHECK_INTERVAL_MS);
}

// -- theme toggle (per device via localStorage) -----------------------------
//
// Cycles auto -> light -> dark. "auto" removes the override so the CSS media
// query (prefers-color-scheme) follows the system/HA theme; light/dark force
// it. The head bootstrap already applied the stored theme before the first
// paint; this only keeps the button label in sync and handles taps.

const THEME_ICON = { auto: "\u{1F317}", light: "☀️", dark: "\u{1F319}" };
const THEME_TITLE = {
  auto: "Farbschema: automatisch",
  light: "Farbschema: hell",
  dark: "Farbschema: dunkel",
};

function applyTheme(theme) {
  // "auto" means "no override" — let the media query decide.
  if (theme === "auto") {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = theme;
  }
  const button = document.getElementById("btn-theme");
  button.textContent = THEME_ICON[theme];
  button.title = THEME_TITLE[theme];
  button.setAttribute("aria-label", THEME_TITLE[theme]);
}

function initTheme() {
  let theme = loadTheme();
  applyTheme(theme);
  document.getElementById("btn-theme").addEventListener("click", () => {
    theme = nextTheme(theme);
    saveTheme(theme);
    applyTheme(theme);
  });
}

function init() {
  initPopover({ onTagsChanged: render });
  initTheme();
  initScreensaver();
  applyAdminVisibility();
  restoreViewState();
  document.getElementById("btn-prev").addEventListener("click", () => navigate(-1));
  document.getElementById("btn-next").addEventListener("click", () => navigate(1));
  document.getElementById("btn-today").addEventListener("click", goToToday);
  document.getElementById("btn-month").addEventListener("click", () => switchView("month"));
  document.getElementById("btn-week").addEventListener("click", () => switchView("week"));
  document
    .getElementById("btn-mode-calendar")
    .addEventListener("click", () => switchMode("calendar"));
  document.getElementById("btn-mode-power").addEventListener("click", () => switchMode("power"));
  attachSwipe(document.getElementById("calendar"), {
    onSwipeLeft: () => navigate(1),
    onSwipeRight: () => navigate(-1),
  });
  window.addEventListener("resize", onWindowResized);
  window.addEventListener("orientationchange", onWindowResized);
  render();
  refresh();
  setInterval(refresh, REFRESH_INTERVAL_MS);
}

init();
