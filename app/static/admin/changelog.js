// Change-log admin section (Änderungsprotokoll). Shows the last four weeks
// of calendar changes in both directions: incoming (source →
// Familienkalender) and outgoing (Belegt-Sync → Xalt). Foreign strings
// (source names, appointment titles) go into the DOM exclusively via
// textContent.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

const DIRECTION_LABELS = { in: "eingehend", out: "ausgehend" };
const ACTION_LABELS = { added: "hinzugefügt", updated: "geändert", removed: "entfernt" };
const ACTION_CLASSES = {
  added: "changelog-added",
  updated: "changelog-updated",
  removed: "changelog-removed",
};

/** German label for a change direction ("in"/"out"). */
export function directionLabel(direction) {
  return DIRECTION_LABELS[direction] ?? direction;
}

/** German label for a change action ("added"/"updated"/"removed"). */
export function actionLabel(action) {
  return ACTION_LABELS[action] ?? action;
}

/** CSS class carrying the (theme-aware) accent color for an action. */
export function actionClass(action) {
  return ACTION_CLASSES[action] ?? "";
}

function pad2(number) {
  return String(number).padStart(2, "0");
}

/**
 * Change timestamp as a local "TT.MM. HH:MM" string. `iso` is an ISO-8601
 * UTC string; the result uses the browser's local time (Europe/Berlin on
 * the kiosk, like the rest of the frontend). Invalid input yields "".
 */
export function formatEntryTime(iso) {
  if (!iso) return "";
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return "";
  const date = `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}.`;
  const time = `${pad2(moment.getHours())}:${pad2(moment.getMinutes())}`;
  return `${date} ${time}`;
}

/**
 * Appointment start (storage-encoded: ISO date for all-day, ISO datetime
 * otherwise) as a short local "TT.MM." string. All-day dates are parsed from
 * their components to avoid a timezone off-by-one. Empty/invalid → "".
 */
export function formatEventDate(encoded) {
  if (!encoded) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(encoded)) {
    const [, month, day] = encoded.split("-");
    return `${day}.${month}.`;
  }
  const moment = new Date(encoded);
  if (Number.isNaN(moment.getTime())) return "";
  return `${pad2(moment.getDate())}.${pad2(moment.getMonth() + 1)}.`;
}

export function renderChangelog(listNode, entries) {
  listNode.replaceChildren();
  if (!entries.length) {
    listNode.append(el("li", "hint", "Keine Änderungen in den letzten 4 Wochen."));
    return;
  }
  for (const entry of entries) {
    const row = el("li", "changelog-item");
    row.append(el("span", "changelog-time", formatEntryTime(entry.ts)));
    row.append(
      el(
        "span",
        `changelog-direction changelog-dir-${entry.direction}`,
        directionLabel(entry.direction),
      ),
    );
    row.append(el("span", "changelog-scope", entry.scope));
    row.append(
      el("span", `changelog-action ${actionClass(entry.action)}`, actionLabel(entry.action)),
    );
    const eventDate = formatEventDate(entry.event_start);
    const title = eventDate ? `${entry.title} (${eventDate})` : entry.title;
    row.append(el("span", "changelog-title", title));
    listNode.append(row);
  }
}

export async function loadChangelog() {
  const { entries } = await api.getChangelog();
  renderChangelog(byId("changelog-list"), entries);
}

export function initChangelog() {
  byId("btn-changelog-refresh").addEventListener("click", async () => {
    showMessage(byId("changelog-message"), "");
    try {
      await loadChangelog();
      showMessage(byId("changelog-message"), "Aktualisiert.");
    } catch (error) {
      showMessage(byId("changelog-message"), error.message, true);
    }
  });
}
