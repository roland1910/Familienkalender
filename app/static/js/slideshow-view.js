// Full-screen photo slideshow (kiosk screensaver). Shows one photo at a
// time, object-fit: contain on black, advancing every SLIDE_INTERVAL_MS.
// The next image is preloaded before the swap so there is no black flash,
// and the swap is a soft cross-fade (two stacked <img> layers).
//
// Photos come from GET /api/slideshow/next ({id, name}); the image bytes
// from api/slideshow/image/{id}. The `name` is only ever set via
// textContent (foreign filename) — see the caption element below.

import { fetchNextPhoto, photoImageUrl } from "./api.js";
import { el } from "./dom.js";

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
 * Load an image URL and resolve once it is decoded (so the swap never
 * shows a half-loaded frame). Rejects on error so the caller can skip a
 * broken/vanished photo and try the next one.
 */
function preload(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    // Detach the handlers once they have fired so the Image and its closures
    // become collectable. On a 30s kiosk cadence this runs thousands of times
    // over weeks; leaving the listeners attached would pin every previous
    // Image (and this closure) for the lifetime of the page.
    img.onload = () => {
      img.onload = img.onerror = null;
      resolve(url);
    };
    img.onerror = () => {
      img.onload = img.onerror = null;
      reject(new Error(`Bild konnte nicht geladen werden: ${url}`));
    };
    img.src = url;
  });
}

function buildOverlay() {
  const node = el("div", "slideshow-overlay");
  const layerA = el("div", "slideshow-layer");
  const layerB = el("div", "slideshow-layer");
  const caption = el("p", "slideshow-caption");
  node.append(layerA, layerB, caption);
  layers = [layerA, layerB];
  activeLayer = 0;
  return { node, caption };
}

async function showNext(caption) {
  let photo;
  try {
    photo = await fetchNextPhoto();
  } catch {
    return; // keep the current image; retry on the next tick
  }
  if (!running) return;
  const url = photoImageUrl(photo.id);
  try {
    await preload(url);
  } catch {
    return; // broken/vanished file — skip, next tick fetches another
  }
  if (!running) return;
  const next = (activeLayer + 1) % 2;
  // Cross-fade: paint the new image onto the inactive layer, fade it in, fade
  // the old one out. The now-inactive layer keeps its old background-image
  // (we never clear it), but this is not a leak: with only two layers, at any
  // time only the two most-recently-shown image URLs are referenced, so the
  // browser holds at most two decoded images — not a growing set.
  layers[next].style.backgroundImage = `url("${cssUrl(url)}")`;
  layers[next].classList.add("slideshow-layer-visible");
  layers[activeLayer].classList.remove("slideshow-layer-visible");
  activeLayer = next;
  // Foreign filename → textContent only (never markup).
  caption.textContent = photo.name ?? "";
}

// Escape the characters that could break out of the CSS url("...") context.
// The URL itself is api/slideshow/image/<number> (id is a number), so this
// is belt-and-suspenders against a future path-shaped id.
function cssUrl(url) {
  return url.replace(/["\\]/g, "\\$&");
}

/** Start the slideshow inside `container` (idempotent). */
export function startSlideshow(container) {
  stopSlideshow();
  running = true;
  const built = buildOverlay();
  overlay = built.node;
  container.append(overlay);
  showNext(built.caption);
  timer = setInterval(() => showNext(built.caption), SLIDE_INTERVAL_MS);
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
