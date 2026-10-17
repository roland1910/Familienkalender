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
// Operating mode (Etappe 49): a touch does not end the slideshow straight
// away any more. The first touch brings up three large corner icons — mark
// for deletion, turn left, turn right — and keeps the picture on screen; a
// touch BESIDE them ends it as before. Their visibility and that "ends it"
// window are the same five seconds on purpose, so there is never a state in
// which the icons are up but a tap next to them does nothing.
//
// Items come from GET /api/slideshow/next ({id, name, kind, taken, folders,
// rotation});
// the bytes from api/slideshow/image/{id} (which supports byte ranges, so the
// browser can seek/stream a video instead of downloading it whole). The
// `name`/`folders` are foreign strings and are only ever set via textContent.

import { fetchNextPhoto, markPhotoForDeletion, photoImageUrl, rotatePhoto } from "./api.js";
import { el } from "./dom.js";
import { createIcon } from "./icons.js";
import { formatFolderTrail, formatTakenAt } from "./slideshow-format.js";
import {
  advanceDelayMs,
  CONTROLS_TIMEOUT_MS,
  errorDelayMs,
  isVideoMedia,
  MAX_VIDEO_MS,
  rotationStyle,
  SLIDE_INTERVAL_MS,
} from "./slideshow-media.js";

// Fade duration; kept in sync with the CSS transition on .slideshow-layer.
const FADE_MS = 800;

// How long a short note ("Zum Löschen vorgemerkt.") stays up. Outlives the
// advance it triggers on purpose — the confirmation belongs to the tap, not
// to the picture.
const NOTICE_MS = 2500;

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

// -- operating mode (Etappe 49) ---------------------------------------------
//
// A touch on the running slideshow brings up three big corner icons (mark
// for deletion, turn left, turn right) for CONTROLS_TIMEOUT_MS; a touch
// beside them ends the slideshow as before, and every tap on an icon
// restarts the window. While the icons are up the slideshow MUST NOT
// advance — otherwise the picture changes mid-tap and the wrong photo is
// turned or thrown away. That means three things have to be held: the
// pending advance timer, a playing video, and a swap that is already in
// flight (its fetch finished during the tap).

let context = null; // the built overlay parts, for callers without one
let controlsNode = null;
let noticeNode = null;

let controlsTimer = null;
let noticeTimer = null;
// The advance that is armed (or was armed when the icons came up), so it can
// be re-armed unchanged once they go away.
let pendingAdvance = null;
// A video paused by the operating mode, to be resumed with it.
let pausedVideo = null;
// Resolvers of showNext() calls that are waiting for the icons to go away.
let controlsWaiters = [];
// The item currently on screen: what the icons act on.
let currentMedia = null;

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
  // The clip is gone (or paused for good) — nothing left to resume.
  pausedVideo = null;
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

/**
 * One operating icon: a large round touch target with an inline SVG. The
 * German name lives on aria-label/title — the SVG itself is decorative
 * (never an emoji; the kiosk has no font for those, see icons.js).
 */
function buildControl(id, iconName, label, extraClass) {
  const button = el("button", `slideshow-control ${extraClass}`);
  button.type = "button";
  button.id = id;
  button.setAttribute("aria-label", label);
  button.title = label;
  const icon = createIcon(iconName);
  if (icon !== null) button.append(icon);
  return button;
}

function buildControls() {
  const node = el("div", "slideshow-controls");
  node.hidden = true;
  // Positions are Roland's: bin top right, turn left bottom left, turn right
  // bottom right — the two turns mirror the direction they stand for.
  node.append(
    buildControl(
      "slideshow-mark-delete",
      "trash",
      "Dieses Bild zum Löschen vormerken",
      "slideshow-control-delete",
    ),
    buildControl(
      "slideshow-rotate-ccw",
      "rotate-ccw",
      "Bild gegen den Uhrzeigersinn drehen",
      "slideshow-control-ccw",
    ),
    buildControl(
      "slideshow-rotate-cw",
      "rotate-cw",
      "Bild im Uhrzeigersinn drehen",
      "slideshow-control-cw",
    ),
  );
  node.querySelector("#slideshow-mark-delete").addEventListener("click", () => {
    markCurrentForDeletion();
  });
  node.querySelector("#slideshow-rotate-ccw").addEventListener("click", () => {
    turnCurrent("ccw");
  });
  node.querySelector("#slideshow-rotate-cw").addEventListener("click", () => {
    turnCurrent("cw");
  });
  return node;
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
  // Short confirmations ("Zum Löschen vorgemerkt.") — announced, because the
  // picture it refers to is gone by the time it appears.
  const notice = el("p", "slideshow-notice");
  notice.hidden = true;
  notice.setAttribute("role", "status");
  const controls = buildControls();
  node.append(layerA, layerB, caption, takenAt, folderTrail, notice, controls);
  layers = [layerA, layerB];
  activeLayer = 0;
  controlsNode = controls;
  noticeNode = notice;
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

/**
 * Advance after `delay` ms; replaces any pending advance. While the
 * operating icons are up the request is only REMEMBERED — `resumeAdvance`
 * arms it once they go away, so the picture can never change under a finger
 * that is about to tap an icon.
 */
function scheduleNext(built, delay) {
  clearTimer();
  pendingAdvance = { built, delay };
  if (controlsAreVisible()) return;
  timer = setTimeout(() => {
    timer = null;
    // Defensive: the icons may have come up in the same tick the timer
    // fired. Leave `pendingAdvance` in place so it is re-armed later.
    if (controlsAreVisible()) return;
    pendingAdvance = null;
    showNext(built);
  }, delay);
}

function clearControlsTimer() {
  if (controlsTimer !== null) {
    clearTimeout(controlsTimer);
    controlsTimer = null;
  }
}

/** (Re)start the window the icons are visible — and a tap ends — for. */
function armControls() {
  clearControlsTimer();
  controlsTimer = setTimeout(hideControls, CONTROLS_TIMEOUT_MS);
}

/**
 * Whether the icons are genuinely on screen — read from the DOM, never from
 * a remembered flag.
 *
 * The decision "does this tap open the icons or end the slideshow?" has to
 * agree with what the finger is aiming at. A separate boolean is a second
 * truth that can drift from the screen, and when it does the kiosk does the
 * opposite of what it looks like: no icons, yet the tap counts as the second
 * one and drops back to the calendar (Roland, 17.10.2026). The DOM cannot
 * drift from itself.
 */
// Safety net (17.10.2026, after Roland's display stopped reacting at all):
// the kiosk must NEVER swallow a touch. In normal operation a tap that opens
// the icons is followed either by a tap on an icon or by one that ends the
// slideshow — the icons stay up for CONTROLS_TIMEOUT_MS, so the "open the
// icons" branch cannot be reached twice in a row inside that window. If it
// is, the icons are demonstrably not reaching the screen, and the honest
// answer is to hand the tap back: landing in the calendar is always better
// than tapping a display that does nothing. Reset whenever the icons are
// properly taken down again, so an ordinary second cycle is unaffected.
let lastOpenAt = 0;

function controlsAreVisible() {
  return controlsNode !== null && !controlsNode.hidden && controlsNode.isConnected;
}

/** Show the icons. Returns whether they are actually up now. */
function showControls() {
  if (controlsNode === null) return false;
  controlsNode.hidden = false;
  clearTimer(); // `pendingAdvance` still holds what was armed
  pauseVideoForControls();
  armControls();
  return controlsAreVisible();
}

function hideControls() {
  lastOpenAt = 0; // the icons behaved: the next tap may open them again
  if (controlsNode !== null) controlsNode.hidden = true;
  clearControlsTimer();
  resumeAdvance();
}

function pauseVideoForControls() {
  const current = layers[activeLayer];
  if (current !== undefined && current.tagName === "VIDEO" && !current.paused) {
    current.pause();
    pausedVideo = current;
  }
}

function resumeAdvance() {
  if (pausedVideo !== null) {
    const video = pausedVideo;
    pausedVideo = null;
    const started = video.play();
    // A clip that refuses to resume is not allowed to hang the slideshow —
    // the re-armed cap below (MAX_VIDEO_MS) moves on regardless.
    if (started && typeof started.catch === "function") started.catch(() => {});
  }
  const pending = pendingAdvance;
  if (pending !== null) scheduleNext(pending.built, pending.delay);
  releaseControlsWaiters();
}

function releaseControlsWaiters() {
  const waiters = controlsWaiters;
  controlsWaiters = [];
  for (const resolve of waiters) resolve();
}

/** Resolves as soon as the operating icons are gone (at once if they are). */
function whenControlsHidden() {
  if (!controlsAreVisible()) return Promise.resolve();
  return new Promise((resolve) => controlsWaiters.push(resolve));
}

/**
 * Feed a global interaction into the running slideshow. Returns true when
 * the slideshow handled it (and must stay up), false when the caller should
 * end it — the historical behaviour.
 *
 * Only POINTER input opens the operating mode. Keyboard and wheel keep
 * ending the slideshow at once: the icons are corner touch targets with no
 * keyboard affordance, so a key press would only insert a step nobody at the
 * kiosk can use — and on a maintenance laptop "any key returns to the
 * calendar" is exactly what is expected.
 */
export function handleSlideshowInteraction(event) {
  if (!running) return false;
  if (event?.type !== "pointerdown") return false;
  if (!controlsAreVisible()) {
    const now = Date.now();
    const openingIsBroken = lastOpenAt !== 0 && now - lastOpenAt < CONTROLS_TIMEOUT_MS;
    lastOpenAt = now;
    // Second attempt in a row to open icons that never stayed up: give up on
    // them and let the caller end the slideshow (see lastOpenAt above).
    if (openingIsBroken) return false;
    // Otherwise claim the tap only if the icons are now genuinely up.
    // Claiming a tap that showed nothing would swallow every touch.
    return showControls();
  }
  const target = event.target;
  const onControl = target instanceof Element && target.closest(".slideshow-controls") !== null;
  if (!onControl) return false; // a tap beside the icons ends the slideshow
  armControls(); // tapping an icon buys another window
  return true;
}

function showNotice(text) {
  if (noticeNode === null) return;
  noticeNode.textContent = text;
  noticeNode.hidden = false;
  if (noticeTimer !== null) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => {
    noticeTimer = null;
    if (noticeNode !== null) {
      noticeNode.textContent = "";
      noticeNode.hidden = true;
    }
  }, NOTICE_MS);
}

/** Apply a stored rotation to one layer (see rotationStyle for the maths). */
function applyRotation(element, rotation) {
  const frame =
    overlay === null ? null : { width: overlay.clientWidth, height: overlay.clientHeight };
  const style = rotationStyle(rotation, frame);
  element.style.width = style.width;
  element.style.height = style.height;
  element.style.transform = style.transform;
}

async function turnCurrent(direction) {
  armControls();
  const media = currentMedia;
  if (media === null) return;
  try {
    const result = await rotatePhoto(media.id, direction);
    // The item may have been replaced while the request was in flight.
    if (!running || currentMedia?.id !== media.id) return;
    currentMedia.rotation = result.rotation;
    applyRotation(layers[activeLayer], result.rotation);
  } catch {
    showNotice("Drehen fehlgeschlagen.");
  }
}

async function markCurrentForDeletion() {
  armControls();
  const media = currentMedia;
  if (media === null) return;
  try {
    await markPhotoForDeletion(media.id);
  } catch {
    showNotice("Vormerken fehlgeschlagen.");
    return;
  }
  if (!running) return;
  showNotice("Zum Löschen vorgemerkt.");
  // The backend stops handing a marked photo out, so leaving it on screen
  // would contradict the tap — move on at once. Hiding the icons first
  // releases the hold on the advance; the pending one is dropped because
  // this advance replaces it.
  hideControls();
  pendingAdvance = null;
  if (context !== null) scheduleNext(context, 0);
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
  // The icons may have come up while this item was loading. Swapping now
  // would change the picture under the finger that is aiming at them, so
  // the already-loaded item waits in the invisible layer until they go.
  await whenControlsHidden();
  if (!running) return;

  // The previous video (if any) is done the moment the new layer takes over.
  releaseCurrentVideo();
  // Cross-fade: fade the freshly loaded layer in, the old one out. The
  // now-inactive layer keeps its old src (we never clear it), but this is not
  // a leak: with only two layers, at any time only the two most-recently-shown
  // URLs are referenced, so the browser holds at most two decoded items — not
  // a growing set.
  // The stored rotation belongs to this item; apply it before the fade so
  // the picture never appears upright for a frame and then jumps.
  applyRotation(element, media.rotation);
  currentMedia = { id: media.id, rotation: media.rotation };
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
  context = built;
  container.append(overlay);
  showNext(built);
}

/** Stop the slideshow and remove its overlay. */
export function stopSlideshow() {
  running = false;
  clearTimer();
  clearControlsTimer();
  if (noticeTimer !== null) {
    clearTimeout(noticeTimer);
    noticeTimer = null;
  }
  // Detach the video listeners and pause before the overlay goes away, so no
  // handler can fire (and no decoding can continue) after the teardown.
  releaseCurrentVideo();
  if (overlay !== null) {
    overlay.remove();
    overlay = null;
  }
  layers = [];
  context = null;
  controlsNode = null;
  noticeNode = null;
  lastOpenAt = 0;
  pendingAdvance = null;
  currentMedia = null;
  // Anything still waiting for the icons to go away resumes and then bails
  // out on the `running` check — no promise is left dangling.
  releaseControlsWaiters();
}

/** Whether the slideshow is currently on screen (for main.js/idle logic). */
export function isSlideshowRunning() {
  return running;
}

// Timing exported for the timing-aware callers/tests.
export { FADE_MS, MAX_VIDEO_MS, SLIDE_INTERVAL_MS };
