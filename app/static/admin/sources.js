// Source list: rendering with status/counts plus the inline controls
// (display mode, enable/disable, rename, two-step delete).

import * as api from "./api.js";
import { byId, el, withPageError } from "./dom.js";

const TYPE_LABELS = { google: "Google", caldav: "Nextcloud" };

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

export async function refreshSources() {
  const { sources } = await api.getSources();
  const list = byId("source-list");
  list.replaceChildren();
  if (sources.length === 0) {
    list.append(el("li", "source-item empty", "Noch keine Kalender-Quellen eingerichtet."));
    return;
  }
  for (const source of sources) list.append(renderSource(source));
}
