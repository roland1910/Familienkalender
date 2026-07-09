// Busy-Sync admin section (MoreValue -> Xalt). Lets Roland connect a
// SEPARATE write token (calendar.events scope) for his Xalt account, pick
// which source(s) are mirrored as neutral "Busy MV" blocks, and toggle the
// sync on/off. Foreign strings (source names, error messages, status) go
// into the DOM exclusively via textContent.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

// Claim ticket is not needed here: the write target is fixed to the primary
// calendar, so write-connect stores the token straight away.

// Tracks the enabled flag last loaded from the backend, so the toggle
// button (which flips it) and the save button (which keeps it as-is while
// saving the source selection) both act on the current value.
let currentEnabled = false;

function renderSources(container, sources, selectedIds) {
  container.replaceChildren();
  if (sources.length === 0) {
    container.append(el("p", "hint", "Noch keine Quellen angelegt."));
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

function selectedSourceIds() {
  return [...byId("busy-source-list").querySelectorAll("input:checked")].map((cb) =>
    Number(cb.value),
  );
}

// Pure status-line formatter (DOM-free, node-testable). ``locale`` is
// injectable so tests are locale-independent.
export function formatStatus(status, { locale = "de-DE" } = {}) {
  if (!status?.last_run) return "Noch nie gelaufen.";
  const when = new Date(status.last_run).toLocaleString(locale);
  if (status.error) {
    return `Letzter Lauf ${when}: Fehler – ${status.error}`;
  }
  return `Letzter Lauf ${when}: ${status.active_blocks} aktive Belegt-Blöcke.`;
}

// Pure label/state for the prominent on/off button (DOM-free, node-testable).
export function toggleButtonLabel(enabled) {
  return enabled ? "Belegt-Sync ist AN – ausschalten" : "Belegt-Sync ist AUS – einschalten";
}

function renderToggleButton(enabled) {
  const button = byId("btn-busy-toggle");
  button.textContent = toggleButtonLabel(enabled);
  button.classList.toggle("busy-toggle-on", enabled);
  button.setAttribute("aria-pressed", String(enabled));
}

export async function loadBusySync() {
  const { busy_sync: data } = await api.getBusySync();
  byId("busy-connected").textContent = data.connected
    ? "Schreib-Verbindung: verbunden."
    : "Schreib-Verbindung: nicht verbunden.";
  byId("btn-busy-disconnect").hidden = !data.connected;
  currentEnabled = Boolean(data.enabled);
  renderToggleButton(currentEnabled);
  renderSources(byId("busy-source-list"), data.sources, data.source_ids);
  showMessage(byId("busy-status"), formatStatus(data.status), Boolean(data.status?.error));
}

async function startWriteAuth() {
  const { auth_url: authUrl } = await api.busyWriteAuthUrl();
  const link = byId("busy-auth-link");
  link.href = authUrl;
  link.hidden = false;
  byId("busy-connect-step").hidden = false;
}

export function initBusySync() {
  byId("btn-busy-connect").addEventListener("click", async () => {
    showMessage(byId("busy-message"), "");
    try {
      await startWriteAuth();
    } catch (error) {
      showMessage(byId("busy-message"), error.message, true);
    }
  });

  byId("btn-busy-code").addEventListener("click", async () => {
    showMessage(byId("busy-message"), "");
    try {
      await api.busyWriteConnect(byId("busy-code").value);
      byId("busy-code").value = "";
      byId("busy-connect-step").hidden = true;
      byId("busy-auth-link").hidden = true;
      await loadBusySync();
      showMessage(byId("busy-message"), "Schreib-Verbindung hergestellt.");
    } catch (error) {
      showMessage(byId("busy-message"), error.message, true);
    }
  });

  byId("btn-busy-disconnect").addEventListener("click", async () => {
    showMessage(byId("busy-message"), "");
    try {
      await api.disconnectBusyWrite();
      await loadBusySync();
      showMessage(byId("busy-message"), "Schreib-Verbindung getrennt.");
    } catch (error) {
      showMessage(byId("busy-message"), error.message, true);
    }
  });

  byId("btn-busy-save").addEventListener("click", async () => {
    showMessage(byId("busy-message"), "");
    try {
      await api.saveBusySync(currentEnabled, selectedSourceIds());
      await loadBusySync();
      showMessage(byId("busy-message"), "Auswahl gespeichert.");
    } catch (error) {
      showMessage(byId("busy-message"), error.message, true);
    }
  });

  byId("btn-busy-toggle").addEventListener("click", async () => {
    showMessage(byId("busy-message"), "");
    try {
      const nextEnabled = !currentEnabled;
      await api.saveBusySync(nextEnabled, selectedSourceIds());
      await loadBusySync();
      showMessage(
        byId("busy-message"),
        nextEnabled ? "Belegt-Sync eingeschaltet." : "Belegt-Sync ausgeschaltet.",
      );
    } catch (error) {
      showMessage(byId("busy-message"), error.message, true);
    }
  });
}
