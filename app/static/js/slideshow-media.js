// Pure media rules for the slideshow (no DOM): what kind of layer an item
// from /api/slideshow/next needs, and how long to wait before advancing.
// Extracted out of slideshow-view.js so the timing decisions are unit-
// testable with plain node --test — the view itself is all DOM and timers.
//
// The window overrides exist so the E2E tests can shrink the durations
// without waiting the real 30/60 seconds; they are read once at module load
// (the tests inject them before the app's modules run).

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
