// Pure media rules for the slideshow (no DOM): what kind of layer an item
// from /api/slideshow/next needs, how long to wait before advancing, and how
// a stored rotation turns into CSS for a layer. Extracted out of
// slideshow-view.js so these decisions are unit-testable with plain
// node --test — the view itself is all DOM and timers.
//
// The window overrides exist so the E2E tests can shrink the durations
// without waiting the real 30/60/5 seconds; they are read once at module
// load (the tests inject them before the app's modules run).

/** How long a photo stays on screen. */
export const SLIDE_INTERVAL_MS = globalThis.SLIDESHOW_INTERVAL_MS ?? 30000;

/**
 * Hard cap on a single video. A long clip must not monopolise the
 * screensaver, and a video that stalls (a codec the kiosk half-supports,
 * a slow CIFS read) must never leave the slideshow standing still: the cap
 * fires even if neither `ended` nor `error` ever does.
 */
export const MAX_VIDEO_MS = globalThis.SLIDESHOW_MAX_VIDEO_MS ?? 60000;

/**
 * Delay before retrying after a video failed to load or play (unsupported
 * codec, unreadable file). Short, so a broken clip is skipped essentially
 * at once — but not zero: with an index full of unplayable files, a
 * zero-delay retry would spin into a hot request loop.
 */
export const VIDEO_ERROR_RETRY_MS = 1000;

/** Whether an item from /api/slideshow/next needs a <video> layer. */
export function isVideoMedia(media) {
  return media?.kind === "video";
}

/**
 * How long to leave a successfully shown item up before fetching the next
 * one. For a video this is only the upper bound — its `ended` event
 * normally advances earlier.
 */
export function advanceDelayMs(media) {
  return isVideoMedia(media) ? MAX_VIDEO_MS : SLIDE_INTERVAL_MS;
}

/**
 * How long to wait after an item could not be shown at all. Photos keep the
 * historical behaviour (skip, try again on the next regular tick); a broken
 * video is retried quickly so the screen never sits black on a bad file.
 */
export function errorDelayMs(media) {
  return isVideoMedia(media) ? VIDEO_ERROR_RETRY_MS : SLIDE_INTERVAL_MS;
}

// -- operating mode at the kiosk (Etappe 49) --------------------------------

/**
 * How long the three operating icons (mark for deletion, turn left, turn
 * right) stay on screen after a touch — and, deliberately, exactly how long
 * a touch BESIDE them still ends the slideshow. One window, not two
 * overlapping ones: there must never be a state in which the icons are
 * visible but a tap next to them does nothing. Every tap on an icon restarts
 * it, so two turns in a row are possible without the bar running away.
 */
export const CONTROLS_TIMEOUT_MS = globalThis.SLIDESHOW_CONTROLS_MS ?? 5000;

/** The nearest of the four quarter turns; anything unusable is "upright". */
export function normalizeRotation(value) {
  const degrees = Number(value);
  if (!Number.isFinite(degrees)) return 0;
  return (((Math.round(degrees / 90) * 90) % 360) + 360) % 360;
}

/**
 * The CSS a layer needs to show its item turned by `rotation` degrees inside
 * a `frame` of {width, height} pixels.
 *
 * The fiddly part: the layers are <img>/<video> with `object-fit: contain`,
 * so the browser fits the picture to the LAYER BOX and only then is the box
 * turned. Rotating a 1920x1080 layer by 90° leaves a picture that was fitted
 * against the wrong edges — it sticks out left and right and wastes the
 * height. For a quarter turn the layer's own edges are therefore swapped
 * (it becomes 1080x1920, i.e. fitted against the *other* edge) before it
 * turns about its own centre back into the frame. 0° and 180° map the box
 * onto itself and need the turn alone.
 *
 * The layer is centred (`top/left: 50%`), hence the constant
 * `translate(-50%, -50%)` in front of the rotation.
 *
 * This is an EXTRA correction on top of the EXIF orientation the browser
 * already applies (`image-orientation: from-image`, Etappe 32) — it does not
 * fight it, it turns whatever the browser decided to show.
 */
export function rotationStyle(rotation, frame) {
  const quarter = normalizeRotation(rotation);
  const transform = `translate(-50%, -50%) rotate(${quarter}deg)`;
  const width = Number(frame?.width);
  const height = Number(frame?.height);
  const measurable = Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0;
  if ((quarter !== 90 && quarter !== 270) || !measurable) {
    // Percentages keep the layer glued to the overlay (and keep following it
    // across a resize) — and they are the only drawable answer when the
    // frame cannot be measured yet.
    return { width: "100%", height: "100%", transform };
  }
  return { width: `${height}px`, height: `${width}px`, transform };
}
