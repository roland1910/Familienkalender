// Unit tests for the pure media rules of the slideshow: which layer an item
// needs and how long to wait before advancing. The timing decisions are what
// keep the kiosk from ever standing still on a broken or overlong video.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  advanceDelayMs,
  errorDelayMs,
  isVideoMedia,
  MAX_VIDEO_MS,
  SLIDE_INTERVAL_MS,
  VIDEO_ERROR_RETRY_MS,
} from "../../app/static/js/slideshow-media.js";

test("isVideoMedia: only the explicit video kind counts", () => {
  assert.equal(isVideoMedia({ kind: "video" }), true);
  assert.equal(isVideoMedia({ kind: "image" }), false);
});

test("isVideoMedia: missing or malformed input is not a video", () => {
  // The response is untrusted-ish input; anything unexpected must fall back
  // to the image path, which is the safe one (no autoplay, fixed interval).
  assert.equal(isVideoMedia(undefined), false);
  assert.equal(isVideoMedia(null), false);
  assert.equal(isVideoMedia({}), false);
  assert.equal(isVideoMedia({ kind: "VIDEO" }), false);
  assert.equal(isVideoMedia({ kind: "" }), false);
});

test("advanceDelayMs: photos use the slide interval", () => {
  assert.equal(advanceDelayMs({ kind: "image" }), SLIDE_INTERVAL_MS);
  assert.equal(advanceDelayMs({}), SLIDE_INTERVAL_MS);
});

test("advanceDelayMs: videos are capped, never left to run forever", () => {
  assert.equal(advanceDelayMs({ kind: "video" }), MAX_VIDEO_MS);
});

test("errorDelayMs: a broken video is retried quickly", () => {
  assert.equal(errorDelayMs({ kind: "video" }), VIDEO_ERROR_RETRY_MS);
});

test("errorDelayMs: a broken photo keeps the historical slow retry", () => {
  assert.equal(errorDelayMs({ kind: "image" }), SLIDE_INTERVAL_MS);
});

test("the retry delay is short but never zero (no hot request loop)", () => {
  assert.ok(VIDEO_ERROR_RETRY_MS > 0);
  assert.ok(VIDEO_ERROR_RETRY_MS < SLIDE_INTERVAL_MS);
});

test("defaults: 30s per photo, at most 60s per video", () => {
  assert.equal(SLIDE_INTERVAL_MS, 30000);
  assert.equal(MAX_VIDEO_MS, 60000);
});
