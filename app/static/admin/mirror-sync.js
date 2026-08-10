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

// German explanation for a run the backend's data-loss guard held back.
// The backend stores a code (same split as slideshow_scan_status /
// scanWarningText), the sentence lives here. Pure, node-testable.
const SKIP_TEXTS = {
  source_error:
    "eine ausgewählte Quelle konnte in diesem Lauf nicht gelesen werden — " +
    "es wurde nichts gelöscht. Sobald die Quelle wieder liefert, gleicht der " +
    "nächste Lauf normal ab.",
  empty_result:
    "die ausgewählten Kalender lieferten keine Termine, obwohl Kopien " +
    "vorhanden sind — zur Sicherheit wurde nichts gelöscht.",
  no_sources:
    "es ist keine Quelle ausgewählt, obwohl Kopien vorhanden sind — zur " +
    "Sicherheit wurde nichts gelöscht. Zum bewussten Beenden bitte den " +
    "Spiegel-Sync ausschalten.",
};

export function skipWarningText(status) {
  if (!status?.skipped) return "";
  const reason = SKIP_TEXTS[status.skip_reason];
  return reason
    ? `Letzter Lauf übersprungen: ${reason}`
    : "Letzter Lauf übersprungen — es wurde nichts geändert.";
}

// Pure label for the "remove the copies" button. Destructive and hard to
// undo, so it takes two taps: the first one arms it, the second acts.
export function cleanupButtonLabel(armed) {
  return armed
    ? "Wirklich alle Kopien entfernen? Zum Bestätigen erneut tippen"
    : "Kopien im Zielkalender entfernen";
}

// Pure German note about a queued (or given-up) cleanup of a calendar that is
// no longer the target. The backend only reports counts/flags.
export function cleanupNoteText(data) {
  if (data?.cleanup_failed) {
    return (
      "Die bisherigen Kopien konnten nicht entfernt werden — bitte im alten " +
      "Kalender manuell prüfen und dort aufräumen."
    );
  }
  if (data?.cleanup_pending) {
    return (
      "Aufräumen vorgemerkt: Die Kopien im bisherigen Ziel-Kalender werden " +
      "beim nächsten Sync entfernt."
    );
  }
  return "";
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
  renderCleanupButton(false);
  renderTargets(byId("mirror-target"), data.targets, data.target_source_id);
  renderSources(byId("mirror-source-list"), lastSources, data.source_ids, data.target_source_id);
  showMessage(byId("mirror-status"), formatStatus(data.status), Boolean(data.status?.error));
  // A held-back run is not an error — shown as its own, dezente warning line.
  showMessage(byId("mirror-skip"), skipWarningText(data.status), true);
  showMessage(byId("mirror-cleanup-note"), cleanupNoteText(data), true);
}

// Whether the destructive cleanup button is armed (waiting for the second,
// confirming tap). Reset on every reload of the section.
let cleanupArmed = false;

function renderCleanupButton(armed) {
  cleanupArmed = armed;
  const button = byId("btn-mirror-cleanup");
  button.textContent = cleanupButtonLabel(armed);
  button.classList.toggle("danger", armed);
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
      // Deselecting the LAST source switches the mirror off in the same
      // step. Otherwise "no source selected" would be a silent instruction
      // to delete every copy — and the backend cannot tell that apart from a
      // lost/unreadable setting, so it refuses to act on it anyway. Stopping
      // the mirror stays a deliberate act via the on/off button.
      const emptied = selectedSourceIds().length === 0;
      await save(emptied ? false : currentEnabled);
      showMessage(
        byId("mirror-message"),
        emptied
          ? "Auswahl gespeichert. Ohne Quelle wurde der Spiegel-Sync ausgeschaltet."
          : "Auswahl gespeichert.",
      );
    } catch (error) {
      showMessage(byId("mirror-message"), error.message, true);
    }
  });

  byId("btn-mirror-cleanup").addEventListener("click", async () => {
    showMessage(byId("mirror-message"), "");
    // First tap only arms the button — removing every copy from a real
    // calendar must never be one stray tap away.
    if (!cleanupArmed) {
      renderCleanupButton(true);
      return;
    }
    try {
      await api.cleanupMirrorSync();
      await loadMirrorSync();
      showMessage(
        byId("mirror-message"),
        "Der Spiegel-Sync wurde ausgeschaltet. Die Kopien werden beim nächsten Sync entfernt.",
      );
    } catch (error) {
      renderCleanupButton(false);
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
