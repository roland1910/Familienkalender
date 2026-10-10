// Admin "Zum Löschen vorgemerkt" section (Etappe 49): the photos somebody
// flagged at the kiosk, with a preview, the file path and a final decision
// per entry — delete for good (two taps) or keep after all.
//
// Nothing is deleted at the kiosk itself: the bin icon there only sets a
// flag. This list is where it becomes irreversible, so the destructive
// button follows the same two-tap pattern as the mirror cleanup, and a
// failed deletion LEAVES THE ENTRY IN PLACE with its error next to it — the
// file is still lying on the NAS, and a row that quietly vanished would
// pretend otherwise.
//
// Filename, path and folder names are foreign strings from a network share
// and go into the DOM exclusively via textContent.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

// Pure label for the two-tap destructive button (DOM-free, node-testable).
export function deleteButtonLabel(armed) {
  return armed ? "Wirklich löschen? Zum Bestätigen erneut tippen" : "Endgültig löschen";
}

// Pure German line for the moment the photo was flagged. Anything the Date
// constructor cannot read yields "" — never "Invalid Date".
export function formatMarkedAt(iso, options = {}) {
  if (typeof iso !== "string" || iso === "") return "";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const { locale = "de-DE", ...rest } = options;
  return `Vorgemerkt am ${when.toLocaleString(locale, rest)}`;
}

function renderEntry(item) {
  const row = el("li", "pending-delete-item");

  // The preview comes from the ordinary slideshow image endpoint — the id
  // is ours, from the list above, never a client-supplied path.
  const preview = document.createElement("img");
  preview.className = "pending-delete-preview";
  preview.src = `api/slideshow/image/${encodeURIComponent(item.id)}`;
  // Decorative: the filename stands right next to it in text.
  preview.alt = "";
  preview.loading = "lazy";
  row.append(preview);

  const info = el("div", "pending-delete-info");
  info.append(el("p", "pending-delete-name", item.name ?? ""));
  // Roland asked explicitly to see where the picture lives.
  info.append(el("p", "pending-delete-path", item.path ?? ""));
  info.append(el("p", "pending-delete-marked", formatMarkedAt(item.marked_at)));
  const entryMessage = el("p", "message");
  entryMessage.hidden = true;
  info.append(entryMessage);
  row.append(info);

  const buttons = el("div", "pending-delete-actions");
  const remove = el("button", "action-button", deleteButtonLabel(false));
  remove.type = "button";
  remove.dataset.photoId = String(item.id);
  const keep = el("button", "action-button subtle", "Doch behalten");
  keep.type = "button";
  keep.dataset.photoId = String(item.id);
  buttons.append(remove, keep);
  row.append(buttons);

  let armed = false;
  const disarm = () => {
    armed = false;
    remove.textContent = deleteButtonLabel(false);
    remove.classList.remove("danger");
  };

  remove.addEventListener("click", async () => {
    showMessage(entryMessage, "");
    if (!armed) {
      armed = true;
      remove.textContent = deleteButtonLabel(true);
      remove.classList.add("danger");
      return;
    }
    remove.disabled = true;
    keep.disabled = true;
    try {
      await api.deletePendingPhoto(item.id);
      await loadPendingDeletes();
    } catch (error) {
      // 502: Home Assistant did not confirm the unlink. The file is still
      // there, so the entry stays — with the reason at the entry.
      disarm();
      remove.disabled = false;
      keep.disabled = false;
      showMessage(entryMessage, error.message, true);
    }
  });

  keep.addEventListener("click", async () => {
    showMessage(entryMessage, "");
    keep.disabled = true;
    try {
      await api.keepPendingPhoto(item.id);
      await loadPendingDeletes();
    } catch (error) {
      keep.disabled = false;
      showMessage(entryMessage, error.message, true);
    }
  });

  return row;
}

function render(items) {
  const list = byId("pending-delete-list");
  list.replaceChildren();
  if (items.length === 0) {
    list.append(el("li", "pending-delete-item empty", "Keine Fotos zum Löschen vorgemerkt."));
    return;
  }
  for (const item of items) list.append(renderEntry(item));
}

export async function loadPendingDeletes() {
  const payload = await api.getPendingDeletes();
  render(payload.items ?? []);
}

export function initPendingDeletes() {
  byId("btn-pending-deletes-reload").addEventListener("click", async () => {
    const messageNode = byId("pending-deletes-message");
    showMessage(messageNode, "");
    try {
      await loadPendingDeletes();
    } catch (error) {
      showMessage(messageNode, error.message, true);
    }
  });
}
