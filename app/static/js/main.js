// App wiring: navigation, data loading, auto-refresh, view rendering.

import {
  fetchConfig,
  fetchEvents,
  fetchMe,
  fetchSources,
  fetchTagOptions,
  fetchTags,
} from "./api.js";
import { createConfigRetry } from "./config-retry.js";
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
import {
  loadScreensaverChoice,
  resolveScreensaverEnabled,
  saveScreensaverEnabled,
} from "./screensaver-memory.js";
import { isSlideshowRunning, startSlideshow, stopSlideshow } from "./slideshow-view.js";
import { state } from "./state.js";
import { loadTheme, nextTheme, saveTheme } from "./theme-memory.js";
import { loadViewState, resolveInitialView, saveViewState } from "./view-memory.js";
import { startWeatherView, stopWeatherView } from "./weather-view.js";
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
  if (state.mode === "weather") return "Wetter";
  if (state.view === "month") {
    return `${MONTH_NAMES[state.anchor.getMonth()]} ${state.anchor.getFullYear()}`;
  }
  const { start, end } = weekRange(state.anchor);
  return `KW ${isoWeekNumber(start)} · ${formatDayMonth(start)} – ${formatDayMonth(end)}`;
}

// Marks one option of a group as the selected one: visually via the CSS
// class and, because colour alone is not an accessible state, via
// aria-pressed.
function setSelected(id, selected) {
  const button = document.getElementById(id);
  button.classList.toggle("active", selected);
  button.setAttribute("aria-pressed", String(selected));
}

function render() {
  document.getElementById("period-title").textContent = periodTitle();
  setSelected("btn-month", state.view === "month");
  setSelected("btn-week", state.view === "week");
  if (!state.loaded) return; // keep the loading indicator until first data
  const container = document.getElementById("calendar");
  if (state.view === "month") {
    renderMonthView(container, state.anchor, state.events, today(), state.tags);
  } else {
    renderWeekView(container, state.anchor, state.events, today(), state.tags);
  }
  // The legend belongs to the calendar views only — never to the power or
  // weather view.
  const legend = document.getElementById("legend");
  if (state.mode !== "calendar") {
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

// The three top-level modes and the DOM they own: the toggle button, the
// section shown, and the body class that hides the calendar-only toolbar
// controls via CSS.
const MODES = [
  { mode: "calendar", button: "btn-mode-calendar", section: "calendar" },
  { mode: "power", button: "btn-mode-power", section: "power", bodyClass: "mode-power" },
  { mode: "weather", button: "btn-mode-weather", section: "weather", bodyClass: "mode-weather" },
];

function switchMode(mode) {
  if (state.mode === mode) return;
  closeDayPopover();
  state.mode = mode;
  persistViewState();
  for (const entry of MODES) {
    const active = entry.mode === mode;
    setSelected(entry.button, active);
    document.getElementById(entry.section).hidden = !active;
    if (entry.bodyClass) document.body.classList.toggle(entry.bodyClass, active);
  }
  // Each non-calendar view owns polling timers; only the active one runs.
  if (mode === "power") startPowerView(document.getElementById("power"));
  else stopPowerView();
  if (mode === "weather") startWeatherView(document.getElementById("weather"));
  else stopWeatherView();
  if (mode === "calendar") refresh();
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
// of silently falling back to the calendar. Returns whether a valid
// per-device state was restored — if not, the server default view applies
// (see applyServerDefaultView).
function restoreViewState() {
  const saved = loadViewState();
  if (!saved) return false; // missing or invalid -> keep the defaults
  state.view = saved.view;
  state.anchor = saved.anchor;
  // view and mode are independent enums (see view-memory.js), so restoring
  // view/anchor above is a plain field assignment. mode instead goes
  // through switchMode, deliberately: it is the only place that knows how
  // to toggle a non-calendar view's DOM/lifecycle (starting startPowerView
  // / startWeatherView, toggling body classes, hiding #calendar). This
  // means switchMode also
  // calls persistViewState() and render() again here, writing back and
  // re-rendering the very view/anchor/mode combination that was just
  // loaded — a redundant but idempotent re-persist/render (init() calls
  // render()/refresh() again right after restoreViewState() anyway), not a
  // real user-triggered transition.
  if (saved.mode !== "calendar") switchMode(saved.mode);
  return true;
}

// -- server defaults from /api/config ---------------------------------------
//
// The kiosk browser loses its localStorage on every restart, so anything
// the device has no own choice for comes from the server: the default
// calendar view (admin setting "Standard-Ansicht") and the screensaver
// default. The first fetch is awaited BEFORE the first render, so the
// kiosk starts straight in the configured view with no month->week
// flicker. Boot race: when kiosk and add-on start together after a Pi
// reboot the fetch can fail — then the built-in fallbacks (month,
// screensaver off) render immediately and createConfigRetry keeps trying
// in the background; a late response is only applied while the user has
// not interacted yet (see markInteraction). Deliberately never persisted:
// only real user choices go to localStorage, so a later change of the
// server defaults still reaches devices without their own choice.

let configRetry = null;
// Whether a valid per-device view state was restored (then the server
// default view never applies on this load).
let viewRestored = false;
// The per-device screensaver choice (true/false) or null when the device
// has none — only then does the server default arm/disarm the toggle.
let screensaverChoice = null;
// Set after init()'s first render: a config applied later (retry after
// the boot race) must re-render into the now-known default view.
let initialRenderDone = false;

function applyServerDefaults(config) {
  if (!viewRestored) {
    const defaultView = resolveInitialView(null, config.default_view);
    const viewChanged = defaultView !== state.view;
    state.view = defaultView;
    if (viewChanged && initialRenderDone) {
      render();
      refresh();
    }
  }
  if (screensaverChoice === null) {
    screensaverEnabled = resolveScreensaverEnabled(null, config.screensaver_default);
    applyScreensaverButton();
  }
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
  // The user is actively using the device — a late /api/config response
  // (boot-race retry) must never flip the view or arm the screensaver
  // under their fingers.
  configRetry?.markInteraction();
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
  // The tap is an explicit per-device choice — persist it and stop any
  // pending server default from overriding it (belt and braces: the tap's
  // pointerdown already ran markInteraction).
  screensaverChoice = screensaverEnabled;
  saveScreensaverEnabled(screensaverEnabled);
  applyScreensaverButton();
  // Turning it off while the slideshow is up ends it right away.
  if (!screensaverEnabled) stopSlideshow();
  lastInteractionAt = Date.now();
}

function initScreensaver() {
  screensaverChoice = loadScreensaverChoice();
  // No server default yet — a missing choice starts OFF until /api/config
  // arrives (applyServerDefaults may then arm the toggle).
  screensaverEnabled = resolveScreensaverEnabled(screensaverChoice, null);
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

// Three distinct symbols express the three states without a word (Etappe
// 37): half-disc for auto, sun for light, moon for dark. aria-label/title
// still spell the state out for assistive tech and tooltips.
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
  button.querySelector(".btn-icon").textContent = THEME_ICON[theme];
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

async function init() {
  initPopover({ onTagsChanged: render });
  initTheme();
  initScreensaver();
  applyAdminVisibility();
  viewRestored = restoreViewState();
  // Fetch the server defaults only when something still needs them; the
  // first attempt is awaited (no flicker on the happy path), retries after
  // a boot-race failure run in the background (see applyServerDefaults).
  if (!viewRestored || screensaverChoice === null) {
    configRetry = createConfigRetry({ fetchConfig, applyDefaults: applyServerDefaults });
    await configRetry.start();
  }
  document.getElementById("btn-prev").addEventListener("click", () => navigate(-1));
  document.getElementById("btn-next").addEventListener("click", () => navigate(1));
  document.getElementById("btn-today").addEventListener("click", goToToday);
  document.getElementById("btn-month").addEventListener("click", () => switchView("month"));
  document.getElementById("btn-week").addEventListener("click", () => switchView("week"));
  document
    .getElementById("btn-mode-calendar")
    .addEventListener("click", () => switchMode("calendar"));
  document.getElementById("btn-mode-power").addEventListener("click", () => switchMode("power"));
  document
    .getElementById("btn-mode-weather")
    .addEventListener("click", () => switchMode("weather"));
  attachSwipe(document.getElementById("calendar"), {
    onSwipeLeft: () => navigate(1),
    onSwipeRight: () => navigate(-1),
  });
  window.addEventListener("resize", onWindowResized);
  window.addEventListener("orientationchange", onWindowResized);
  render();
  refresh();
  initialRenderDone = true;
  setInterval(refresh, REFRESH_INTERVAL_MS);
}

init();
