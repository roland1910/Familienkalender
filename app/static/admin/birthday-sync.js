// Birthday-sync admin section. Lets Roland pick which contact sources are
// written out, into which of the two calendars (Google/Xalt and/or a
// Nextcloud calendar), and switch the whole thing on/off. Foreign strings
// (source names, status, error messages) go into the DOM exclusively via
// textContent.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

// The CalDAV target is picked from the CONFIGURED sources, never typed as a
// URL: the backend writes into that source's already validated collection.
const NO_TARGET = "";

// Tracks the enabled flag last loaded from the backend, so the toggle button
// (which flips it) and the save button (which keeps it as-is while saving the
// selection) both act on the current value.
let currentEnabled = false;

function renderSources(container, sources, selectedIds) {
  container.replaceChildren();
  if (sources.length === 0) {
    container.append(el("p", "hint", "Noch keine Geburtstags-Quelle angelegt (Google-Kontakte)."));
    return;
  }
  for (const source of sources) {
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
  const none = el("option", "", "— kein Nextcloud-Ziel —");
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
  return [...byId("birthday-source-list").querySelectorAll("input:checked")].map((cb) =>
    Number(cb.value),
  );
}

function selectedTargetId() {
  const raw = byId("birthday-caldav-target").value;
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
  return (
    `Letzter Lauf ${when}: ${status.active_google} Serien in Xalt, ` +
    `${status.active_caldav} in Nextcloud.${conflicts}`
  );
}

// Pure label/state for the prominent on/off button (DOM-free, node-testable).
export function toggleButtonLabel(enabled) {
  return enabled
    ? "Geburtstags-Sync ist AN – ausschalten"
    : "Geburtstags-Sync ist AUS – einschalten";
}

// German explanation for a run the backend's data-loss guard held back. The
// backend stores a code (same split as slideshow_scan_status /
// scanWarningText), the sentence lives here. Pure, node-testable.
const SKIP_TEXTS = {
  source_error:
    "eine ausgewählte Geburtstags-Quelle konnte in diesem Lauf nicht gelesen " +
    "werden — es wurde nichts gelöscht. Sobald sie wieder liefert, gleicht der " +
    "nächste Lauf normal ab.",
  empty_result:
    "die ausgewählten Kontakte lieferten keine Geburtstage, obwohl Serien " +
    "vorhanden sind — zur Sicherheit wurde nichts gelöscht.",
  no_sources:
    "es ist keine Geburtstags-Quelle ausgewählt, obwohl Serien vorhanden sind " +
    "— zur Sicherheit wurde nichts gelöscht. Zum bewussten Beenden bitte den " +
    "Geburtstags-Sync ausschalten.",
};

export function skipWarningText(status) {
  if (!status?.skipped) return "";
  const reason = SKIP_TEXTS[status.skip_reason];
  return reason
    ? `Letzter Lauf übersprungen: ${reason}`
    : "Letzter Lauf übersprungen — es wurde nichts geändert.";
}

// Pure hint for the Google target (DOM-free, node-testable): the birthday
// sync reuses the busy sync's write token, so without that connection the
// Xalt target cannot work — say so instead of failing silently.
export function googleHintText(googleEnabled, googleConnected) {
  if (googleEnabled && !googleConnected) {
    return (
      "Für den Xalt-Kalender fehlt die Schreib-Verbindung — bitte oben unter " +
      "„Belegt-Sync“ herstellen."
    );
  }
  return "";
}

function renderToggleButton(enabled) {
  const button = byId("btn-birthday-toggle");
  button.textContent = toggleButtonLabel(enabled);
  button.classList.toggle("busy-toggle-on", enabled);
  button.setAttribute("aria-pressed", String(enabled));
}

export async function loadBirthdaySync() {
  const { birthday_sync: data } = await api.getBirthdaySync();
  currentEnabled = Boolean(data.enabled);
  renderToggleButton(currentEnabled);
  byId("birthday-google").checked = Boolean(data.google_enabled);
  renderTargets(byId("birthday-caldav-target"), data.targets, data.caldav_target_source_id);
  renderSources(byId("birthday-source-list"), data.sources, data.source_ids);
  showMessage(byId("birthday-status"), formatStatus(data.status), Boolean(data.status?.error));
  // A held-back run is not an error — shown as its own, dezente warning line.
  showMessage(byId("birthday-skip"), skipWarningText(data.status), true);
  showMessage(
    byId("birthday-google-hint"),
    googleHintText(Boolean(data.google_enabled), Boolean(data.google_connected)),
    true,
  );
}

async function save(enabled) {
  await api.saveBirthdaySync(
    enabled,
    selectedSourceIds(),
    byId("birthday-google").checked,
    selectedTargetId(),
  );
  await loadBirthdaySync();
}

export function initBirthdaySync() {
  byId("btn-birthday-save").addEventListener("click", async () => {
    showMessage(byId("birthday-message"), "");
    try {
      // Deselecting the LAST source switches the sync off in the same step.
      // Otherwise "no source selected" would be a silent instruction to
      // delete every series — and the backend cannot tell that apart from a
      // lost setting, so it refuses to act on it anyway. Stopping stays a
      // deliberate act via the on/off button.
      const emptied = selectedSourceIds().length === 0;
      await save(emptied ? false : currentEnabled);
      showMessage(
        byId("birthday-message"),
        emptied
          ? "Auswahl gespeichert. Ohne Quelle wurde der Geburtstags-Sync ausgeschaltet."
          : "Auswahl gespeichert.",
      );
    } catch (error) {
      showMessage(byId("birthday-message"), error.message, true);
    }
  });

  byId("btn-birthday-toggle").addEventListener("click", async () => {
    showMessage(byId("birthday-message"), "");
    try {
      const nextEnabled = !currentEnabled;
      await save(nextEnabled);
      showMessage(
        byId("birthday-message"),
        nextEnabled ? "Geburtstags-Sync eingeschaltet." : "Geburtstags-Sync ausgeschaltet.",
      );
    } catch (error) {
      showMessage(byId("birthday-message"), error.message, true);
    }
  });
}
