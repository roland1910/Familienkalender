// Admin "Diashow (Kiosk)" section: manage the directories scanned for
// photos and videos, browse the /media tree to pick new ones, show the
// indexed count, switch video playback on/off and trigger a rescan. Foreign
// strings (directory names, error messages) go into the DOM exclusively via
// textContent — see dom.js.
//
// The video switch needs no rescan: videos are always indexed, the setting
// only governs whether /api/slideshow/next may hand one out.
//
// The browser is navigable: clicking a folder descends into it (loading its
// subdirectories from GET /api/admin/slideshow/dirs?path=...), a breadcrumb
// walks back up (never above the /media root — the backend enforces that),
// and "Diesen Ordner hinzufügen" adds the *currently shown* directory to the
// (multi-entry) slideshow_dirs list. Folders without subfolders are still
// addable (photos may sit directly inside).

import * as api from "./api.js";
import { byId, el, showMessage } from "./dom.js";
import { breadcrumbSegments, scanWarningText, shortName } from "./slideshow-path.js";

// The current directory list, kept in sync with the backend so add/remove
// can PUT the whole array.
let currentDirs = [];

// Browser navigation state: the media root (hard boundary), the currently
// shown directory and its subdirectories, filled by the /dirs endpoint.
let mediaRoot = "";
let browsePath = "";
let browseSubdirs = [];

// Generation counter guarding against out-of-order responses: fast repeated
// clicks (folder A, then B) can have their /dirs requests resolve in the
// wrong order. Each navigateTo() call claims the next number; only the call
// still holding the latest number is allowed to apply its result.
let navSeq = 0;

// Photo count plus the discreet warning about directories the last scan
// could not reach (unmounted share) — see scanWarningText.
function renderScanState(payload) {
  byId("slideshow-count").textContent = String(payload.photo_count);
  // "on"/"off"; anything unexpected falls back to the conservative "off"
  // rather than silently leaving the select on a stale value.
  byId("slideshow-videos").value = payload.videos === "on" ? "on" : "off";
  const warning = byId("slideshow-warning");
  const text = scanWarningText(payload);
  warning.textContent = text;
  warning.hidden = text === "";
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

async function saveDirs(dirs, videos) {
  const messageNode = byId("slideshow-message");
  try {
    const payload = await api.saveSlideshowDirs(dirs, videos);
    currentDirs = payload.dirs;
    renderScanState(payload);
    renderDirList();
    updateAddButton();
    showMessage(messageNode, "Gespeichert.");
  } catch (error) {
    showMessage(messageNode, error.message, true);
  }
}

// Save the video switch on its own — the directory list goes along
// unchanged, because the backend PUT always replaces the whole list.
function saveVideos() {
  saveDirs(currentDirs, byId("slideshow-videos").value);
}

function addDir() {
  if (!browsePath) return;
  if (currentDirs.includes(browsePath)) {
    showMessage(byId("slideshow-message"), "Ordner ist bereits ausgewählt.", true);
    return;
  }
  saveDirs([...currentDirs, browsePath]);
}

function removeDir(path) {
  saveDirs(currentDirs.filter((dir) => dir !== path));
}

// Enable/disable "add" so the current folder cannot be added twice.
function updateAddButton() {
  const button = byId("btn-slideshow-add");
  const already = currentDirs.includes(browsePath);
  button.disabled = already || !browsePath;
  button.textContent = already ? "Ordner bereits ausgewählt" : "Diesen Ordner hinzufügen";
}

function renderBreadcrumb() {
  const nav = byId("slideshow-breadcrumb");
  nav.replaceChildren();
  const segments = breadcrumbSegments(mediaRoot, browsePath);
  segments.forEach((segment, index) => {
    const isLast = index === segments.length - 1;
    if (isLast) {
      // The current directory: plain text, not a link back to itself.
      nav.append(el("span", "slideshow-crumb current", segment.name));
    } else {
      const crumb = el("button", "slideshow-crumb", segment.name);
      crumb.type = "button";
      crumb.title = segment.path;
      crumb.addEventListener("click", () => navigateTo(segment.path));
      nav.append(crumb);
      nav.append(el("span", "slideshow-crumb-sep", "›"));
    }
  });
}

function renderBrowseList() {
  const list = byId("slideshow-browse-list");
  list.replaceChildren();
  if (browseSubdirs.length === 0) {
    list.append(el("li", "slideshow-browse-item empty", "Keine Unterordner."));
    return;
  }
  for (const dir of browseSubdirs) {
    const item = el("li", "slideshow-browse-item");
    const enter = el("button", "slideshow-browse-enter");
    enter.type = "button";
    enter.title = dir.path;
    enter.append(el("span", "slideshow-browse-name", dir.name));
    enter.append(el("span", "slideshow-browse-chevron", "›"));
    enter.addEventListener("click", () => navigateTo(dir.path));
    item.append(enter);
    list.append(item);
  }
}

// Load and render the directory at ``path`` (empty = media root). The
// backend returns the resolved base and its subdirectories; it also refuses
// anything outside /media, so the browser cannot escape. The response also
// carries ``parent``, intentionally unused here — the breadcrumb (built from
// mediaRoot + browsePath via breadcrumbSegments) already covers "back"
// navigation without needing the server to repeat it.
async function navigateTo(path) {
  const messageNode = byId("slideshow-message");
  const seq = ++navSeq;
  try {
    const payload = await api.listMediaDirs(path ?? "");
    // Discard stale responses: if another navigateTo() started after this
    // one, its result (or an earlier one still in flight) must win instead.
    if (seq !== navSeq) return;
    mediaRoot = payload.media_root ?? mediaRoot;
    browsePath = payload.base;
    browseSubdirs = payload.dirs;
    renderBreadcrumb();
    renderBrowseList();
    updateAddButton();
  } catch (error) {
    if (seq !== navSeq) return;
    showMessage(messageNode, error.message, true);
  }
}

async function rescan() {
  const messageNode = byId("slideshow-message");
  const button = byId("btn-slideshow-rescan");
  button.disabled = true;
  showMessage(messageNode, "Wird eingelesen…");
  try {
    const payload = await api.rescanSlideshow();
    renderScanState(payload);
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
  mediaRoot = payload.media_root ?? "";
  renderScanState(payload);
  renderDirList();
  // Start the browser at the media root.
  await navigateTo("");
}

export function initSlideshow() {
  byId("btn-slideshow-add").addEventListener("click", addDir);
  byId("btn-slideshow-rescan").addEventListener("click", rescan);
  byId("btn-slideshow-videos").addEventListener("click", saveVideos);
}
