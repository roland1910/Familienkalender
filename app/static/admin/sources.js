// Source list: one card per source — a summary that is always visible and
// a detail that folds out (Etappe 51, Schritt 3).
//
// Before this, seven controls sat in ONE flex row per source: display
// mode, shortcode, colour plus reset, feed switch, precedence, disable,
// rename and delete. With eight sources that is around sixty controls on
// a phone screen, which is exactly the picture Roland sent. The summary
// now answers "which calendar is this, is it healthy" at a glance, and
// everything that changes something lives one tap away.

// The calendar's color resolution — the admin picker must show exactly
// the color the views use (custom color or palette default).
import { colorForSource } from "../js/colors.js";
import * as api from "./api.js";
import { byId, el, withPageError } from "./dom.js";

const TYPE_LABELS = {
  google: "Google",
  caldav: "Nextcloud",
  google_contacts: "Geburtstage",
};

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

function feedPriorityControl(source) {
  // Precedence for collapsing duplicate appointments in the ICS feed:
  // when the same event appears in several feed sources, the one from the
  // source with the higher value survives. Saved on change (blur/Enter);
  // the server validates the range (-100..100).
  const label = el("label", "feed-priority-label", "Vorrang (Abo-Dubletten): ");
  const input = el("input", "feed-priority-input");
  input.type = "number";
  input.step = "1";
  input.min = "-100";
  input.max = "100";
  input.value = String(source.feed_priority);
  input.title =
    "Bei identischen Terminen in mehreren Kalendern erscheint im Abo nur der mit höherem Vorrang.";
  input.addEventListener("change", () => {
    withPageError(async () => {
      const value = Number.parseInt(input.value, 10);
      // Ignore an empty/non-numeric field instead of sending garbage.
      if (!Number.isInteger(value)) {
        input.value = String(source.feed_priority);
        return;
      }
      await api.patchSource(source.id, { feed_priority: value });
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

function nameControl(source) {
  // The name is a plain field, saved on change (blur/Enter). It replaces
  // a button that opened a row with its own save and cancel buttons —
  // three controls become one. An empty name is refused silently and the
  // field snaps back, because the server would reject it anyway.
  const label = el("label", "name-label", "Name: ");
  const input = el("input", "name-input");
  input.type = "text";
  input.value = source.name;
  input.addEventListener("change", () => {
    const name = input.value.trim();
    if (!name) {
      input.value = source.name;
      return;
    }
    if (name === source.name) return;
    withPageError(async () => {
      await api.patchSource(source.id, { name });
      await refreshSources();
    });
  });
  label.append(input);
  return label;
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
  const button = el(
    "button",
    "small-button source-toggle-button",
    source.enabled ? "Deaktivieren" : "Aktivieren",
  );
  button.type = "button";
  button.addEventListener("click", () => {
    withPageError(async () => {
      await api.patchSource(source.id, { enabled: !source.enabled });
      await refreshSources();
    });
  });
  return button;
}

/** Fold every card except `keep` — only one detail is open at a time. */
function foldOthers(keep) {
  for (const item of byId("source-list").querySelectorAll(".source-item.open")) {
    if (item !== keep) setOpen(item, false);
  }
}

function setOpen(item, open) {
  item.classList.toggle("open", open);
  const summary = item.querySelector(".source-summary");
  const controls = item.querySelector(".source-controls");
  if (summary !== null) {
    summary.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (controls !== null) controls.hidden = !open;
}

// Shortcode and precedence only ever act inside the ICS subscription, so
// they live under its switch and appear with it. That takes two fields
// out of the normal case and explains what they are for by position.
function updateFeedFields(item, includeInFeed) {
  for (const node of item.querySelectorAll(".feed-detail")) {
    node.hidden = !includeInFeed;
  }
}

function renderSummary(source) {
  // A button, not a div: keyboard-operable and announced as expandable
  // without any extra wiring.
  const summary = el("button", "source-summary");
  summary.type = "button";
  summary.setAttribute("aria-expanded", "false");

  const head = el("div", "source-head");
  // The colour dot mirrors the chips in the calendar, so the list reads
  // at a glance (colorForSource is loaded here anyway).
  const dot = el("span", "source-dot");
  dot.style.setProperty("--source-color", colorForSource(source));
  head.append(dot, el("strong", "source-name", source.name));
  head.append(el("span", "type-badge", TYPE_LABELS[source.type] || source.type));
  if (!source.enabled) head.append(el("span", "state-badge", "deaktiviert"));
  if (source.last_sync_error) head.append(el("span", "error-badge", "Fehler"));

  const status = el("div", "source-status");
  status.append(
    el("span", "", `Letzter Sync: ${formatTimestamp(source.last_sync_at)}`),
    el("span", "", `${source.event_count} Termine`),
  );

  summary.append(head, status, el("span", "source-expand", "Bearbeiten"));
  return summary;
}

function renderSource(source) {
  const item = el("li", `source-item${source.enabled ? "" : " disabled"}`);
  item.dataset.sourceId = String(source.id);

  const summary = renderSummary(source);
  summary.addEventListener("click", () => {
    const open = !item.classList.contains("open");
    if (open) foldOthers(item);
    setOpen(item, open);
  });
  item.append(summary);

  // The error text stays OUTSIDE the fold: a broken source must say so
  // without being opened first.
  if (source.last_sync_error) {
    item.append(el("div", "source-error error-text", source.last_sync_error));
  }

  const warning = el("div", "source-feed-warning");
  updateFeedWarning(warning, source.display_mode, source.include_in_feed);

  const controls = el("div", "source-controls");
  controls.hidden = true;
  // Birthdays (google_contacts) are all-day events and thus always family
  // relevant — the display mode has no effect, so no selector is shown.
  if (source.type !== "google_contacts") {
    const modeLabel = el("label", "mode-label", "Anzeige: ");
    modeLabel.append(modeSelect(source, warning));
    controls.append(modeLabel);
  }
  controls.append(nameControl(source), colorControl(source));

  const feedBlock = el("div", "source-feed-block");
  const toggle = feedToggle(source, warning);
  const shortcode = shortcodeControl(source);
  const priority = feedPriorityControl(source);
  shortcode.classList.add("feed-detail");
  priority.classList.add("feed-detail");
  feedBlock.append(toggle, shortcode, priority);
  // Instant feedback before the save round-trip re-renders the card.
  toggle.querySelector(".feed-toggle").addEventListener("change", (event) => {
    updateFeedFields(item, event.currentTarget.checked);
  });
  controls.append(feedBlock, warning);

  controls.append(toggleControl(source));
  // Delete never shares a row with anything else and sits below a rule:
  // mis-tapping is solved structurally, not by colour alone.
  const danger = el("div", "source-danger");
  danger.append(deleteControl(source));
  controls.append(danger);

  item.append(controls);
  updateFeedFields(item, source.include_in_feed);
  return item;
}

export async function refreshSources() {
  const list = byId("source-list");
  // Every control saves and then re-renders the list. Without carrying
  // the open card over, changing a colour or a switch would fold the
  // detail away under the user's finger.
  const openId = list.querySelector(".source-item.open")?.dataset.sourceId ?? null;
  const { sources } = await api.getSources();
  list.replaceChildren();
  if (sources.length === 0) {
    list.append(el("li", "source-item empty", "Noch keine Kalender-Quellen eingerichtet."));
    return;
  }
  for (const source of sources) {
    const item = renderSource(source);
    if (openId !== null && item.dataset.sourceId === openId) setOpen(item, true);
    list.append(item);
  }
}
