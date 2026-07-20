// Full-screen photo slideshow (kiosk screensaver). Shows one photo at a
// time, object-fit: contain on black, advancing every SLIDE_INTERVAL_MS.
// The next image is preloaded before the swap so there is no black flash,
// and the swap is a soft cross-fade (two stacked <img> layers).
//
// The layers MUST be real <img> elements, never CSS background-image divs:
// backgrounds ignore the EXIF orientation tag, so portrait photos from a
// phone showed up rotated by 90° on the kiosk display. <img> honours it
// (`image-orientation: from-image`, also set explicitly in the CSS).
//
// Photos come from GET /api/slideshow/next ({id, name}); the image bytes
// from api/slideshow/image/{id}. The `name` is only ever set via
// textContent (foreign filename) — see the caption element below.

import { fetchNextPhoto, photoImageUrl } from "./api.js";
import { el } from "./dom.js";
import { formatFolderTrail, formatTakenAt } from "./slideshow-format.js";

// How long each photo stays on screen. A window override lets the E2E test
// use a short interval without waiting the full duration.
export const SLIDE_INTERVAL_MS = globalThis.SLIDESHOW_INTERVAL_MS ?? 30000;
// Fade duration; kept in sync with the CSS transition on .slideshow-layer.
const FADE_MS = 800;

let overlay = null;
let layers = [];
let activeLayer = 0;
let timer = null;
let running = false;

/**
 * Point a layer at an image URL and resolve once it has finished loading,
 * so the cross-fade never reveals a half-loaded frame. The still-invisible
 * layer doubles as the preloader — no separate Image object needed.
 * Rejects on error so the caller can skip a broken/vanished photo.
 */
function loadIntoLayer(img, url) {
  return new Promise((resolve, reject) => {
    // Re-assigning the same src does not reliably fire `load` again, and the
    // photo index legitimately repeats a URL (single-photo folder, or the
    // wrap-around of a full rotation). Already-decoded means we are done.
    if (img.getAttribute("src") === url && img.complete && img.naturalWidth > 0) {
      resolve();
      return;
    }
    // Detach the handlers once they have fired so their closures become
    // collectable. On a 30s kiosk cadence this runs thousands of times over
    // weeks; leaving them attached would pin every previous closure.
    img.onload = () => {
      img.onload = img.onerror = null;
      resolve();
    };
    img.onerror = () => {
      img.onload = img.onerror = null;
      reject(new Error(`Bild konnte nicht geladen werden: ${url}`));
    };
    img.src = url;
  });
}

function buildLayer() {
  const img = el("img", "slideshow-layer");
  // Decorative: the filename is already announced by the caption element.
  img.alt = "";
  img.draggable = false;
  return img;
}

function buildOverlay() {
  const node = el("div", "slideshow-overlay");
  const layerA = buildLayer();
  const layerB = buildLayer();
  const caption = el("p", "slideshow-caption");
  // Taken-at date (top right) and folder trail (top left) of the visible
  // photo — purely decorative metadata, hidden from assistive tech.
  const takenAt = el("p", "slideshow-taken");
  takenAt.setAttribute("aria-hidden", "true");
  const folderTrail = el("p", "slideshow-folders");
  folderTrail.setAttribute("aria-hidden", "true");
  node.append(layerA, layerB, caption, takenAt, folderTrail);
  layers = [layerA, layerB];
  activeLayer = 0;
  return { node, caption, takenAt, folderTrail };
}

// Set an overlay badge's text (foreign strings → textContent only) and hide
// it entirely when there is nothing to show.
function setBadge(node, text) {
  node.textContent = text;
  node.hidden = text === "";
}

async function showNext({ caption, takenAt, folderTrail }) {
  let photo;
  try {
    photo = await fetchNextPhoto();
  } catch {
    return; // keep the current image; retry on the next tick
  }
  if (!running) return;
  const url = photoImageUrl(photo.id);
  const next = (activeLayer + 1) % 2;
  try {
    // Loads into the still-invisible layer, so this is the preload.
    await loadIntoLayer(layers[next], url);
  } catch {
    return; // broken/vanished file — skip, next tick fetches another
  }
  if (!running) return;
  // Cross-fade: fade the freshly loaded layer in, the old one out. The
  // now-inactive layer keeps its old src (we never clear it), but this is not
  // a leak: with only two layers, at any time only the two most-recently-shown
  // image URLs are referenced, so the browser holds at most two decoded
  // images — not a growing set.
  layers[next].classList.add("slideshow-layer-visible");
  layers[activeLayer].classList.remove("slideshow-layer-visible");
  activeLayer = next;
  // The metadata badges belong to the incoming photo, so they swap in the
  // same frame the cross-fade starts (same treatment as the caption).
  // Foreign filename/folder names → textContent only (never markup).
  caption.textContent = photo.name ?? "";
  setBadge(takenAt, formatTakenAt(photo.taken));
  setBadge(folderTrail, formatFolderTrail(photo.folders));
}

/** Start the slideshow inside `container` (idempotent). */
export function startSlideshow(container) {
  stopSlideshow();
  running = true;
  const built = buildOverlay();
  overlay = built.node;
  container.append(overlay);
  showNext(built);
  timer = setInterval(() => showNext(built), SLIDE_INTERVAL_MS);
}

/** Stop the slideshow and remove its overlay. */
export function stopSlideshow() {
  running = false;
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  if (overlay !== null) {
    overlay.remove();
    overlay = null;
  }
  layers = [];
}

/** Whether the slideshow is currently on screen (for main.js/idle logic). */
export function isSlideshowRunning() {
  return running;
}

// Fade duration exported for the timing-aware caller/tests.
export { FADE_MS };
