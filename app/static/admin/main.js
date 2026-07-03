// Admin page wiring: source list, add wizards (Nextcloud/Google),
// settings, manual sync. Foreign strings (source names, calendar names,
// error messages) are rendered exclusively via textContent.

import * as api from "./api.js";

const TYPE_LABELS = { google: "Google", caldav: "Nextcloud" };

function byId(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showMessage(node, text, isError) {
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle("error-text", Boolean(isError));
}

function formatTimestamp(iso) {
  if (!iso) return "noch nie";
  return new Date(iso).toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// -- source list -------------------------------------------------------------

function modeSelect(source) {
  const select = el("select", "mode-select");
  for (const [value, label] of [
    ["full", "Alle Termine"],
    ["filtered", "Gefiltert (Abend/mehrtägig)"],
  ]) {
    const option = el("option", "", label);
    option.value = value;
    if (value === source.display_mode) option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { display_mode: select.value });
      await refreshSources();
    });
  });
  return select;
}

function renameControl(source, container) {
  const button = el("button", "small-button", "Umbenennen");
  button.type = "button";
  button.addEventListener("click", () => {
    if (container.querySelector(".rename-row")) return;
    const row = el("div", "rename-row");
    const input = el("input");
    input.type = "text";
    input.value = source.name;
    const save = el("button", "small-button", "Speichern");
    save.type = "button";
    save.addEventListener("click", () => {
      withPageError(async () => {
        const name = input.value.trim();
        if (!name) return;
        await api.patchSource(source.id, { name });
        await refreshSources();
      });
    });
    const cancel = el("button", "small-button subtle", "Abbrechen");
    cancel.type = "button";
    cancel.addEventListener("click", () => row.remove());
    row.append(input, save, cancel);
    container.append(row);
    input.focus();
  });
  return button;
}

function deleteControl(source) {
  // Two-step confirmation without native dialogs (touch friendly, testable).
  const button = el("button", "small-button danger", "Löschen");
  button.type = "button";
  let armed = false;
  button.addEventListener("click", () => {
    if (!armed) {
      armed = true;
      button.textContent = "Wirklich löschen?";
      setTimeout(() => {
        armed = false;
        button.textContent = "Löschen";
      }, 5000);
      return;
    }
    withPageError(async () => {
      await api.deleteSource(source.id);
      await refreshSources();
    });
  });
  return button;
}

function toggleControl(source) {
  const button = el("button", "small-button", source.enabled ? "Deaktivieren" : "Aktivieren");
  button.type = "button";
  button.addEventListener("click", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { enabled: !source.enabled });
      await refreshSources();
    });
  });
  return button;
}

function renderSource(source) {
  const item = el("li", `source-item${source.enabled ? "" : " disabled"}`);
  item.dataset.sourceId = String(source.id);

  const head = el("div", "source-head");
  head.append(el("strong", "source-name", source.name));
  head.append(el("span", "type-badge", TYPE_LABELS[source.type] || source.type));
  if (!source.enabled) head.append(el("span", "state-badge", "deaktiviert"));
  if (source.last_sync_error) head.append(el("span", "error-badge", "Fehler"));
  item.append(head);

  const status = el("div", "source-status");
  status.append(
    el("span", "", `Letzter Sync: ${formatTimestamp(source.last_sync_at)}`),
    el("span", "", `${source.event_count} Termine`),
  );
  item.append(status);

  if (source.last_sync_error) {
    item.append(el("div", "source-error error-text", source.last_sync_error));
  }

  const controls = el("div", "source-controls");
  const modeLabel = el("label", "mode-label", "Anzeige: ");
  modeLabel.append(modeSelect(source));
  controls.append(
    modeLabel,
    toggleControl(source),
    renameControl(source, item),
    deleteControl(source),
  );
  item.append(controls);
  return item;
}

async function refreshSources() {
  const { sources } = await api.getSources();
  const list = byId("source-list");
  list.replaceChildren();
  if (sources.length === 0) {
    list.append(el("li", "source-item empty", "Noch keine Kalender-Quellen eingerichtet."));
    return;
  }
  for (const source of sources) list.append(renderSource(source));
}

function withPageError(action) {
  return action().catch((error) => {
    showMessage(byId("page-message"), error.message, true);
  });
}

// -- Nextcloud wizard ---------------------------------------------------------

function resetNextcloudForm() {
  byId("nextcloud-form").hidden = true;
  byId("nc-step-select").hidden = true;
  showMessage(byId("nc-error"), "");
  for (const id of ["nc-url", "nc-username", "nc-password", "nc-name"]) {
    byId(id).value = "";
  }
  byId("nc-calendar").replaceChildren();
}

function initNextcloudWizard() {
  byId("btn-add-nextcloud").addEventListener("click", () => {
    resetNextcloudForm();
    byId("google-form").hidden = true;
    byId("nextcloud-form").hidden = false;
    byId("nc-url").focus();
  });
  byId("nc-cancel").addEventListener("click", resetNextcloudForm);

  byId("nc-test").addEventListener("click", async () => {
    const errorNode = byId("nc-error");
    showMessage(errorNode, "");
    try {
      const { calendars } = await api.probeCaldav(
        byId("nc-url").value.trim(),
        byId("nc-username").value.trim(),
        byId("nc-password").value,
      );
      if (calendars.length === 0) {
        showMessage(errorNode, "Keine Kalender gefunden.", true);
        return;
      }
      const select = byId("nc-calendar");
      select.replaceChildren();
      for (const calendar of calendars) {
        const option = el("option", "", calendar.name);
        option.value = calendar.url;
        select.append(option);
      }
      byId("nc-name").value = calendars[0].name;
      select.addEventListener("change", () => {
        byId("nc-name").value = select.selectedOptions[0]?.textContent ?? "";
      });
      byId("nc-step-select").hidden = false;
    } catch (error) {
      showMessage(errorNode, error.message, true);
    }
  });

  byId("nc-save").addEventListener("click", async () => {
    const errorNode = byId("nc-error");
    showMessage(errorNode, "");
    try {
      await api.createSource({
        type: "caldav",
        name: byId("nc-name").value.trim(),
        display_mode: byId("nc-mode").value,
        config: {
          url: byId("nc-url").value.trim(),
          username: byId("nc-username").value.trim(),
          app_password: byId("nc-password").value,
          calendar_url: byId("nc-calendar").value,
        },
      });
      resetNextcloudForm();
      await refreshSources();
    } catch (error) {
      showMessage(errorNode, error.message, true);
    }
  });
}

// -- Google wizard ------------------------------------------------------------

function resetGoogleForm() {
  byId("google-form").hidden = true;
  byId("g-step-credentials").hidden = true;
  byId("g-step-auth").hidden = true;
  byId("g-step-select").hidden = true;
  byId("g-auth-link").hidden = true;
  showMessage(byId("g-error"), "");
  for (const id of ["g-client-id", "g-client-secret", "g-code", "g-name"]) {
    byId(id).value = "";
  }
  byId("g-calendar").replaceChildren();
}

async function startGoogleAuthStep() {
  byId("g-step-credentials").hidden = true;
  byId("g-step-auth").hidden = false;
  const { auth_url: authUrl } = await api.googleAuthUrl();
  const link = byId("g-auth-link");
  link.href = authUrl;
  link.hidden = false;
}

function initGoogleWizard() {
  byId("btn-add-google").addEventListener("click", async () => {
    resetGoogleForm();
    byId("nextcloud-form").hidden = true;
    byId("google-form").hidden = false;
    try {
      const settings = await api.getSettings();
      if (settings.google_credentials.configured) {
        await startGoogleAuthStep();
      } else {
        byId("g-step-credentials").hidden = false;
      }
    } catch (error) {
      showMessage(byId("g-error"), error.message, true);
    }
  });
  byId("g-cancel").addEventListener("click", resetGoogleForm);

  byId("g-save-credentials").addEventListener("click", async () => {
    const errorNode = byId("g-error");
    showMessage(errorNode, "");
    try {
      await api.saveGoogleCredentials(
        byId("g-client-id").value.trim(),
        byId("g-client-secret").value,
      );
      await startGoogleAuthStep();
    } catch (error) {
      showMessage(errorNode, error.message, true);
    }
  });

  byId("g-connect").addEventListener("click", async () => {
    const errorNode = byId("g-error");
    showMessage(errorNode, "");
    try {
      const { calendars } = await api.googleConnect(byId("g-code").value);
      if (calendars.length === 0) {
        showMessage(errorNode, "Keine Kalender gefunden.", true);
        return;
      }
      const select = byId("g-calendar");
      select.replaceChildren();
      for (const calendar of calendars) {
        const option = el("option", "", calendar.name);
        option.value = calendar.id;
        select.append(option);
      }
      byId("g-name").value = calendars[0].name;
      select.addEventListener("change", () => {
        byId("g-name").value = select.selectedOptions[0]?.textContent ?? "";
      });
      byId("g-step-select").hidden = false;
    } catch (error) {
      showMessage(errorNode, error.message, true);
    }
  });

  byId("g-save").addEventListener("click", async () => {
    const errorNode = byId("g-error");
    showMessage(errorNode, "");
    try {
      await api.createSource({
        type: "google",
        name: byId("g-name").value.trim(),
        display_mode: byId("g-mode").value,
        config: { calendar_id: byId("g-calendar").value },
      });
      resetGoogleForm();
      await refreshSources();
    } catch (error) {
      showMessage(errorNode, error.message, true);
    }
  });
}

// -- settings ------------------------------------------------------------------

async function loadSettings() {
  const settings = await api.getSettings();
  byId("evening-boundary").value = settings.evening_boundary;
}

function initSettings() {
  byId("btn-save-settings").addEventListener("click", async () => {
    const messageNode = byId("settings-message");
    try {
      await api.saveSettings(byId("evening-boundary").value);
      showMessage(messageNode, "Gespeichert.");
    } catch (error) {
      showMessage(messageNode, error.message, true);
    }
  });
}

// -- manual sync ----------------------------------------------------------------

function initSync() {
  const button = byId("btn-sync");
  button.addEventListener("click", async () => {
    const messageNode = byId("page-message");
    button.disabled = true;
    showMessage(messageNode, "Synchronisierung läuft…");
    try {
      const { results } = await api.triggerSync();
      const errors = Object.values(results).filter((error) => error !== null);
      const total = Object.keys(results).length;
      if (errors.length === 0) {
        showMessage(messageNode, `Synchronisierung abgeschlossen (${total} Quellen).`);
      } else {
        showMessage(
          messageNode,
          `Synchronisierung abgeschlossen: ${errors.length} von ${total} Quellen mit Fehlern.`,
          true,
        );
      }
      await refreshSources();
    } catch (error) {
      showMessage(messageNode, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

// -- init -------------------------------------------------------------------------

function init() {
  initNextcloudWizard();
  initGoogleWizard();
  initSettings();
  initSync();
  withPageError(async () => {
    await Promise.all([refreshSources(), loadSettings()]);
  });
}

init();
