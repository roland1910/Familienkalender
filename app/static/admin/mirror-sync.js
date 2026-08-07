// Mirror-sync admin section (Xalt -> MoreValue). Lets Roland pick which
// calendars are copied, into which Nextcloud calendar they are written, and
// switch the whole thing on/off. Foreign strings (source names, status,
// error messages) go into the DOM exclusively via textContent.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

// The target is picked from the CONFIGURED CalDAV sources, never typed as a
// URL: the backend writes into that source's already validated collection.
const NO_TARGET = "";

// Tracks the enabled flag last loaded from the backend, so the toggle button
// (which flips it) and the save button (which keeps it as-is while saving the
// selection) both act on the current value.
let currentEnabled = false;

// Last payload from the backend, so the source list can be re-rendered when
// the target changes (a `hidden` attribute would lose against the flex
// display rule of .busy-source-row — same trap as the weather hint).
let lastSources = [];

function renderSources(container, sources, selectedIds, targetId) {
  container.replaceChildren();
  const candidates = sources.filter((source) => source.id !== targetId);
  if (candidates.length === 0) {
    container.append(el("p", "hint", "Noch keine Quellen angelegt."));
    return;
  }
  for (const source of candidates) {
    const label = el("label", "busy-source-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(source.id);
    checkbox.checked = selectedIds.includes(source.id);
    label.append(checkbox, el("span", "", source.name));
    container.append(label);
  }
}

function renderTargets(select, targets, selectedId) {
  select.replaceChildren();
  const none = el("option", "", "— kein Ziel gewählt —");
  none.value = NO_TARGET;
  select.append(none);
  for (const target of targets) {
    const option = el("option", "", target.name);
    option.value = String(target.id);
    select.append(option);
  }
  select.value = selectedId === null || selectedId === undefined ? NO_TARGET : String(selectedId);
}

function selectedSourceIds() {
  return [...byId("mirror-source-list").querySelectorAll("input:checked")].map((cb) =>
    Number(cb.value),
  );
}

function selectedTargetId() {
  const raw = byId("mirror-target").value;
  return raw === NO_TARGET ? null : Number(raw);
}

// Pure status-line formatter (DOM-free, node-testable). ``locale`` is
// injectable so tests are locale-independent.
export function formatStatus(status, { locale = "de-DE" } = {}) {
  if (!status?.last_run) return "Noch nie gelaufen.";
  const when = new Date(status.last_run).toLocaleString(locale);
  if (status.error) {
    return `Letzter Lauf ${when}: Fehler – ${status.error}`;
  }
  const conflicts = status.conflicts
    ? ` ${status.conflicts} Konflikt(e) – werden beim nächsten Lauf erneut versucht.`
    : "";
  return `Letzter Lauf ${when}: ${status.active_mirrors} gespiegelte Termine.${conflicts}`;
}

// Pure label/state for the prominent on/off button (DOM-free, node-testable).
export function toggleButtonLabel(enabled) {
  return enabled ? "Spiegel-Sync ist AN – ausschalten" : "Spiegel-Sync ist AUS – einschalten";
}

function renderToggleButton(enabled) {
  const button = byId("btn-mirror-toggle");
  button.textContent = toggleButtonLabel(enabled);
  button.classList.toggle("busy-toggle-on", enabled);
  button.setAttribute("aria-pressed", String(enabled));
}

export async function loadMirrorSync() {
  const { mirror_sync: data } = await api.getMirrorSync();
  currentEnabled = Boolean(data.enabled);
  lastSources = data.sources;
  renderToggleButton(currentEnabled);
  renderTargets(byId("mirror-target"), data.targets, data.target_source_id);
  renderSources(byId("mirror-source-list"), lastSources, data.source_ids, data.target_source_id);
  showMessage(byId("mirror-status"), formatStatus(data.status), Boolean(data.status?.error));
}

async function save(enabled) {
  await api.saveMirrorSync(enabled, selectedSourceIds(), selectedTargetId());
  await loadMirrorSync();
}

export function initMirrorSync() {
  // Re-render the source list when the target changes: the target calendar
  // must not offer itself as a source (it would copy its own copies).
  byId("mirror-target").addEventListener("change", () => {
    renderSources(byId("mirror-source-list"), lastSources, selectedSourceIds(), selectedTargetId());
  });

  byId("btn-mirror-save").addEventListener("click", async () => {
    showMessage(byId("mirror-message"), "");
    try {
      await save(currentEnabled);
      showMessage(byId("mirror-message"), "Auswahl gespeichert.");
    } catch (error) {
      showMessage(byId("mirror-message"), error.message, true);
    }
  });

  byId("btn-mirror-toggle").addEventListener("click", async () => {
    showMessage(byId("mirror-message"), "");
    try {
      const nextEnabled = !currentEnabled;
      await save(nextEnabled);
      showMessage(
        byId("mirror-message"),
        nextEnabled ? "Spiegel-Sync eingeschaltet." : "Spiegel-Sync ausgeschaltet.",
      );
    } catch (error) {
      showMessage(byId("mirror-message"), error.message, true);
    }
  });
}
