// Source list: rendering with status/counts plus the inline controls
// (display mode, color, enable/disable, rename, two-step delete).

// The calendar's color resolution — the admin picker must show exactly
// the color the views use (custom color or palette default).
import { colorForSource } from "../js/colors.js";
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

// Shown whenever a source both feeds the ICS subscription and has its
// display mode set to "full" — that combination means the feed carries
// this source completely unfiltered (see docs/backlog.md, Feed-Risiko).
const FEED_FULL_WARNING =
  "Achtung: Diese Quelle erscheint vollständig (ungefiltert) im Kalender-Abo.";

function updateFeedWarning(warning, displayMode, includeInFeed) {
  const showWarning = displayMode === "full" && includeInFeed;
  warning.textContent = showWarning ? FEED_FULL_WARNING : "";
  warning.hidden = !showWarning;
}

function modeSelect(source, warning) {
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
    // Instant feedback before the save round-trip; refreshSources() below
    // re-renders the row afterwards with the same warning, computed fresh
    // from the saved state (see renderSource).
    updateFeedWarning(warning, select.value, source.include_in_feed);
    withPageError(async () => {
      await api.patchSource(source.id, { display_mode: select.value });
      await refreshSources();
    });
  });
  return select;
}

function shortcodeControl(source) {
  // Title prefix for the ICS feed; saved on change (blur/Enter). The
  // server normalizes (trim, uppercase) and validates (max 6, A-Z/0-9).
  const label = el("label", "shortcode-label", "Kürzel: ");
  const input = el("input", "shortcode-input");
  input.type = "text";
  input.maxLength = 6;
  input.value = source.shortcode;
  input.title = "Präfix vor Termintiteln im Kalender-Abo (max. 6 Zeichen, A–Z/0–9)";
  input.addEventListener("change", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { shortcode: input.value });
      await refreshSources();
    });
  });
  label.append(input);
  return label;
}

function feedToggle(source, warning) {
  // Per-source switch: does this source feed the subscribable ICS
  // calendar? The family relevance filter only applies for sources with
  // display mode "filtered" — a "full" source appears unfiltered.
  const label = el("label", "feed-toggle-label");
  const input = el("input", "feed-toggle");
  input.type = "checkbox";
  input.checked = source.include_in_feed;
  input.title =
    source.display_mode === "full"
      ? "Termine dieser Quelle erscheinen UNGEFILTERT im abonnierbaren Kalender-Abo (Anzeigemodus „Alle Termine“)"
      : "Termine dieser Quelle erscheinen gefiltert (Abend/mehrtägig) im abonnierbaren Kalender-Abo";
  input.addEventListener("change", () => {
    updateFeedWarning(warning, source.display_mode, input.checked);
    withPageError(async () => {
      await api.patchSource(source.id, { include_in_feed: input.checked });
      await refreshSources();
    });
  });
  label.append(input, el("span", "", "Im Kalender-Abo"));
  return label;
}

function colorControl(source) {
  // Native color picker, prefilled with the effective color (custom or
  // palette default). Saving sends the picked #rrggbb value; the reset
  // button clears the custom color so the palette applies again.
  const label = el("label", "color-label", "Farbe: ");
  const input = el("input", "color-input");
  input.type = "color";
  input.value = colorForSource(source);
  input.title = "Farbe der Termine dieser Quelle im Kalender";
  input.addEventListener("change", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { color: input.value });
      await refreshSources();
    });
  });
  label.append(input);
  const reset = el("button", "small-button subtle color-reset", "Standardfarbe");
  reset.type = "button";
  reset.title = "Eigene Farbe entfernen — es gilt wieder die Standardfarbe";
  reset.hidden = source.color === "";
  reset.addEventListener("click", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { color: "" });
      await refreshSources();
    });
  });
  label.append(reset);
  return label;
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

  const warning = el("div", "source-feed-warning");
  updateFeedWarning(warning, source.display_mode, source.include_in_feed);

  const controls = el("div", "source-controls");
  const modeLabel = el("label", "mode-label", "Anzeige: ");
  modeLabel.append(modeSelect(source, warning));
  controls.append(
    modeLabel,
    shortcodeControl(source),
    colorControl(source),
    feedToggle(source, warning),
    toggleControl(source),
    renameControl(source, item),
    deleteControl(source),
  );
  item.append(controls, warning);
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
