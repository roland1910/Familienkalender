// Admin "Diashow (Kiosk)" section: manage the directories scanned for
// photos, browse the /media tree to pick new ones, show the indexed count
// and trigger a rescan. Foreign strings (directory names, error messages)
// go into the DOM exclusively via textContent — see dom.js.

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";

// The current directory list, kept in sync with the backend so add/remove
// can PUT the whole array.
let currentDirs = [];

function shortName(path) {
  // Show a readable tail of the path (last segment), full path as title.
  const parts = path.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

function renderDirList() {
  const list = byId("slideshow-dir-list");
  list.replaceChildren();
  if (currentDirs.length === 0) {
    list.append(el("li", "slideshow-dir empty", "Noch keine Ordner ausgewählt."));
    return;
  }
  for (const path of currentDirs) {
    const row = el("li", "slideshow-dir");
    const label = el("span", "slideshow-dir-name", shortName(path));
    label.title = path;
    row.append(label);
    const remove = el("button", "action-button subtle", "Entfernen");
    remove.type = "button";
    remove.addEventListener("click", () => removeDir(path));
    row.append(remove);
    list.append(row);
  }
}

async function saveDirs(dirs) {
  const messageNode = byId("slideshow-message");
  try {
    const payload = await api.saveSlideshowDirs(dirs);
    currentDirs = payload.dirs;
    byId("slideshow-count").textContent = String(payload.photo_count);
    renderDirList();
    showMessage(messageNode, "Gespeichert.");
  } catch (error) {
    showMessage(messageNode, error.message, true);
  }
}

function addDir() {
  const select = byId("slideshow-browse");
  const path = select.value;
  if (!path) return;
  if (currentDirs.includes(path)) {
    showMessage(byId("slideshow-message"), "Ordner ist bereits ausgewählt.", true);
    return;
  }
  saveDirs([...currentDirs, path]);
}

function removeDir(path) {
  saveDirs(currentDirs.filter((dir) => dir !== path));
}

async function loadBrowse() {
  const select = byId("slideshow-browse");
  select.replaceChildren();
  try {
    // Empty path lists the media root's immediate subdirectories.
    const { dirs } = await api.listMediaDirs("");
    if (dirs.length === 0) {
      const option = el("option", null, "Keine Unterordner gefunden");
      option.value = "";
      select.append(option);
      return;
    }
    for (const dir of dirs) {
      const option = el("option", null, dir.name);
      option.value = dir.path;
      select.append(option);
    }
  } catch (error) {
    showMessage(byId("slideshow-message"), error.message, true);
  }
}

async function rescan() {
  const messageNode = byId("slideshow-message");
  const button = byId("btn-slideshow-rescan");
  button.disabled = true;
  showMessage(messageNode, "Wird eingelesen…");
  try {
    const payload = await api.rescanSlideshow();
    byId("slideshow-count").textContent = String(payload.photo_count);
    showMessage(messageNode, `Eingelesen: ${payload.photo_count} Fotos.`);
  } catch (error) {
    showMessage(messageNode, error.message, true);
  } finally {
    button.disabled = false;
  }
}

export async function loadSlideshow() {
  const payload = await api.getSlideshow();
  currentDirs = payload.dirs;
  byId("slideshow-count").textContent = String(payload.photo_count);
  renderDirList();
  await loadBrowse();
}

export function initSlideshow() {
  byId("btn-slideshow-add").addEventListener("click", addDir);
  byId("btn-slideshow-rescan").addEventListener("click", rescan);
}
