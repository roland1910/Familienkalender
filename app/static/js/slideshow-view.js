// Full-screen media slideshow (kiosk screensaver). Shows one photo or video
// at a time, object-fit: contain on black. The next item is loaded into the
// still-invisible layer before the swap so there is no black flash, and the
// swap is a soft cross-fade (two stacked layers).
//
// A photo layer MUST be a real <img>, never a CSS background-image div:
// backgrounds ignore the EXIF orientation tag, so portrait photos from a
// phone showed up rotated by 90° on the kiosk display. <img> honours it
// (`image-orientation: from-image`, also set explicitly in the CSS).
//
// Videos (Etappe 33) render into a <video> layer instead: muted, playsinline,
// no controls — it is a screensaver, nobody operates it. Timing differs from
// photos: a video runs until `ended`, capped at MAX_VIDEO_MS, and any load or
// playback error (a codec the kiosk cannot decode) skips to the next item
// quickly. The slideshow must never stall or sit black on a bad file, so
// every path — fetch failure, load failure, playback error, overlong clip —
// ends in a scheduled advance.
//
// Items come from GET /api/slideshow/next ({id, name, kind, taken, folders});
// the bytes from api/slideshow/image/{id} (which supports byte ranges, so the
// browser can seek/stream a video instead of downloading it whole). The
// `name`/`folders` are foreign strings and are only ever set via textContent.

import { fetchNextPhoto, photoImageUrl } from "./api.js";
import { el } from "./dom.js";
import { formatFolderTrail, formatTakenAt } from "./slideshow-format.js";
import {
  advanceDelayMs,
  errorDelayMs,
  isVideoMedia,
  MAX_VIDEO_MS,
  SLIDE_INTERVAL_MS,
} from "./slideshow-media.js";

// Fade duration; kept in sync with the CSS transition on .slideshow-layer.
const FADE_MS = 800;

let overlay = null;
let layers = [];
let activeLayer = 0;
let timer = null;
let running = false;
// Detaches the listeners of the currently playing video (and pauses it).
// Always call before mounting the next item or tearing the overlay down —
// otherwise a finished clip's `ended` handler could advance the slideshow a
// second time, and on a 30s kiosk cadence the closures would pile up.
let releaseVideo = null;

function clearTimer() {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function releaseCurrentVideo() {
  if (releaseVideo !== null) {
    releaseVideo();
    releaseVideo = null;
  }
}

/**
 * Point an <img> layer at a URL and resolve once it has finished loading, so
 * the cross-fade never reveals a half-loaded frame. The still-invisible layer
 * doubles as the preloader — no separate Image object needed. Rejects on
 * error so the caller can skip a broken/vanished photo.
 */
function loadImage(img, url) {
  return new Promise((resolve, reject) => {
    // Re-assigning the same src does not reliably fire `load` again, and the
    // index legitimately repeats a URL (single-photo folder, or the
    // wrap-around of a full rotation). Already-decoded means we are done.
    if (img.getAttribute("src") === url && img.complete && img.naturalWidth > 0) {
      resolve();
      return;
    }
    // Detach the handlers once they have fired so their closures become
    // collectable. On a 30s kiosk cadence this runs thousands of times over
    // weeks; leaving them attached would pin every previous closure.
    const done = (settle) => (event) => {
      img.onload = img.onerror = null;
      settle(event);
    };
    img.onload = done(resolve);
    img.onerror = done(() => reject(new Error(`Bild konnte nicht geladen werden: ${url}`)));
    img.src = url;
  });
}

/**
 * Load a video far enough to show its first frame (`loadeddata`), so the
 * cross-fade starts on a picture and not on black. Rejects on a load error
 * (unsupported codec, unreadable file) — the caller then skips the clip.
 * Unlike the image path this always reloads: a repeated URL must start over
 * from the beginning rather than sit on the last frame of the previous run.
 */
function loadVideo(video, url) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      video.removeEventListener("loadeddata", onLoaded);
      video.removeEventListener("error", onError);
    };
    const onLoaded = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error(`Video konnte nicht geladen werden: ${url}`));
    };
    video.addEventListener("loadeddata", onLoaded);
    video.addEventListener("error", onError);
    video.src = url;
    video.load();
  });
}

function buildImageLayer() {
  const img = el("img", "slideshow-layer");
  // Decorative: the filename is already announced by the caption element.
  img.alt = "";
  img.draggable = false;
  return img;
}

function buildVideoLayer() {
  const video = el("video", "slideshow-layer");
  // Muted + playsinline are what make autoplay permissible at all; controls
  // stay off because the screensaver is not operated, only watched.
  video.muted = true;
  video.defaultMuted = true;
  video.autoplay = true;
  video.controls = false;
  video.playsInline = true;
  video.setAttribute("playsinline", "");
  video.setAttribute("aria-hidden", "true");
  return video;
}

function buildOverlay() {
  const node = el("div", "slideshow-overlay");
  const layerA = buildImageLayer();
  const layerB = buildImageLayer();
  const caption = el("p", "slideshow-caption");
  // Taken-at date and folder trail of the visible item — purely decorative
  // metadata, hidden from assistive tech. Like the caption they run
  // vertically along the screen edges (see the CSS for the exact layout).
  const takenAt = el("p", "slideshow-taken");
  takenAt.setAttribute("aria-hidden", "true");
  const folderTrail = el("p", "slideshow-folders");
  folderTrail.setAttribute("aria-hidden", "true");
  node.append(layerA, layerB, caption, takenAt, folderTrail);
  layers = [layerA, layerB];
  activeLayer = 0;
  return { node, caption, takenAt, folderTrail };
}

/**
 * The element for slot `index`, of the tag the next item needs. An existing
 * element of the right kind is reused (the common all-photos case causes no
 * DOM churn at all); otherwise a fresh one replaces it in place, keeping the
 * layer order — and therefore the cross-fade — intact.
 */
function layerElementFor(index, wantVideo) {
  const current = layers[index];
  const wantedTag = wantVideo ? "VIDEO" : "IMG";
  if (current.tagName === wantedTag) return current;
  const replacement = wantVideo ? buildVideoLayer() : buildImageLayer();
  if (current.classList.contains("slideshow-layer-visible")) {
    replacement.classList.add("slideshow-layer-visible");
  }
  current.replaceWith(replacement);
  layers[index] = replacement;
  return replacement;
}

// Set an overlay badge's text (foreign strings → textContent only) and hide
// it entirely when there is nothing to show.
function setBadge(node, text) {
  node.textContent = text;
  node.hidden = text === "";
}

/**
 * Start playback and arm everything that can end this video: its own `ended`
 * event, a playback `error`, and the MAX_VIDEO_MS cap. Whichever fires first
 * advances the slideshow exactly once — `releaseVideo` detaches the rest.
 */
function playVideo(video, media, built) {
  // Exactly one of the three outcomes may advance the slideshow; whichever
  // comes first wins and the rest become no-ops.
  let settled = false;
  const finish = (delay) => {
    if (settled || !running) return;
    settled = true;
    releaseCurrentVideo();
    scheduleNext(built, delay);
  };
  const onEnded = () => finish(0);
  const onError = () => finish(errorDelayMs(media));
  video.addEventListener("ended", onEnded);
  video.addEventListener("error", onError);
  releaseVideo = () => {
    video.removeEventListener("ended", onEnded);
    video.removeEventListener("error", onError);
    video.pause();
  };
  // The cap is the only guard against a clip that neither ends nor errors
  // (a stalled CIFS read). Armed before playback starts so an immediate
  // `ended`/`error` can supersede it rather than the other way round.
  scheduleNext(built, MAX_VIDEO_MS);
  // Muted autoplay is normally allowed; if the browser still refuses, treat
  // it like any other unplayable clip and move on instead of freezing.
  const started = video.play();
  if (started && typeof started.catch === "function") {
    started.catch(onError);
  }
}

/** Advance after `delay` ms; replaces any pending advance. */
function scheduleNext(built, delay) {
  clearTimer();
  timer = setTimeout(() => {
    timer = null;
    showNext(built);
  }, delay);
}

async function showNext(built) {
  if (!running) return;
  const { caption, takenAt, folderTrail } = built;
  let media;
  try {
    media = await fetchNextPhoto();
  } catch {
    // Keep the current item on screen and retry on the next regular tick.
    scheduleNext(built, SLIDE_INTERVAL_MS);
    return;
  }
  if (!running) return;

  const isVideo = isVideoMedia(media);
  const url = photoImageUrl(media.id);
  const next = (activeLayer + 1) % 2;
  const element = layerElementFor(next, isVideo);
  try {
    // Loads into the still-invisible layer, so this doubles as the preload.
    await (isVideo ? loadVideo(element, url) : loadImage(element, url));
  } catch {
    // Broken, vanished or undecodable file — skip it. A bad video is retried
    // quickly so the screen never sits still on it.
    if (running) scheduleNext(built, errorDelayMs(media));
    return;
  }
  if (!running) return;

  // The previous video (if any) is done the moment the new layer takes over.
  releaseCurrentVideo();
  // Cross-fade: fade the freshly loaded layer in, the old one out. The
  // now-inactive layer keeps its old src (we never clear it), but this is not
  // a leak: with only two layers, at any time only the two most-recently-shown
  // URLs are referenced, so the browser holds at most two decoded items — not
  // a growing set.
  layers[next].classList.add("slideshow-layer-visible");
  layers[activeLayer].classList.remove("slideshow-layer-visible");
  activeLayer = next;
  // The metadata badges belong to the incoming item, so they swap in the
  // same frame the cross-fade starts (same treatment as the caption).
  // Foreign filename/folder names → textContent only (never markup).
  caption.textContent = media.name ?? "";
  setBadge(takenAt, formatTakenAt(media.taken));
  setBadge(folderTrail, formatFolderTrail(media.folders));

  if (isVideo) {
    playVideo(element, media, built);
  } else {
    scheduleNext(built, advanceDelayMs(media));
  }
}

/** Start the slideshow inside `container` (idempotent). */
export function startSlideshow(container) {
  stopSlideshow();
  running = true;
  const built = buildOverlay();
  overlay = built.node;
  container.append(overlay);
  showNext(built);
}

/** Stop the slideshow and remove its overlay. */
export function stopSlideshow() {
  running = false;
  clearTimer();
  // Detach the video listeners and pause before the overlay goes away, so no
  // handler can fire (and no decoding can continue) after the teardown.
  releaseCurrentVideo();
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

// Timing exported for the timing-aware callers/tests.
export { FADE_MS, MAX_VIDEO_MS, SLIDE_INTERVAL_MS };
