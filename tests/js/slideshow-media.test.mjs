// Unit tests for the pure media rules of the slideshow: which layer an item
// needs and how long to wait before advancing. The timing decisions are what
// keep the kiosk from ever standing still on a broken or overlong video.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  advanceDelayMs,
  CONTROLS_TIMEOUT_MS,
  errorDelayMs,
  isVideoMedia,
  MAX_VIDEO_MS,
  normalizeRotation,
  rotationStyle,
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

test("the operating icons stay up for five seconds", () => {
  // One window, not two: the icons are visible for exactly as long as a tap
  // beside them still ends the slideshow (Roland's correction).
  assert.equal(CONTROLS_TIMEOUT_MS, 5000);
});

// -- stored rotation (Etappe 49) --------------------------------------------

test("normalizeRotation: the four quarter turns pass through unchanged", () => {
  for (const quarter of [0, 90, 180, 270]) {
    assert.equal(normalizeRotation(quarter), quarter);
  }
});

test("normalizeRotation: anything else lands on a quarter turn", () => {
  assert.equal(normalizeRotation(-90), 270);
  assert.equal(normalizeRotation(450), 90);
  assert.equal(normalizeRotation(360), 0);
  assert.equal(normalizeRotation("90"), 90);
  assert.equal(normalizeRotation(46), 90);
  assert.equal(normalizeRotation(37), 0);
});

test("normalizeRotation: unusable input is no rotation at all", () => {
  // The value arrives from the backend; anything surprising must show the
  // picture upright rather than produce a NaN transform.
  for (const value of [undefined, null, "", "links", Number.NaN, {}]) {
    assert.equal(normalizeRotation(value), 0);
  }
});

const FRAME = { width: 1920, height: 1080 };

test("rotationStyle: an upright picture fills the frame", () => {
  assert.deepEqual(rotationStyle(0, FRAME), {
    width: "100%",
    height: "100%",
    transform: "translate(-50%, -50%) rotate(0deg)",
  });
});

test("rotationStyle: a half turn keeps the frame, only turns", () => {
  // 180° maps the box onto itself — no edge swap needed.
  assert.deepEqual(rotationStyle(180, FRAME), {
    width: "100%",
    height: "100%",
    transform: "translate(-50%, -50%) rotate(180deg)",
  });
});

test("rotationStyle: a quarter turn SWAPS the layer's edges", () => {
  // The core of the whole feature: object-fit: contain fits the picture to
  // the layer BEFORE the rotation. Turned by 90° inside an unchanged
  // 1920x1080 layer it would stick out sideways. The layer therefore
  // becomes 1080x1920 (the other edge), is fitted against that, and only
  // then turns about its own centre back into the frame.
  assert.deepEqual(rotationStyle(90, FRAME), {
    width: "1080px",
    height: "1920px",
    transform: "translate(-50%, -50%) rotate(90deg)",
  });
  assert.deepEqual(rotationStyle(270, FRAME), {
    width: "1080px",
    height: "1920px",
    transform: "translate(-50%, -50%) rotate(270deg)",
  });
});

test("rotationStyle: a portrait frame swaps the other way round", () => {
  assert.deepEqual(rotationStyle(90, { width: 800, height: 1280 }), {
    width: "1280px",
    height: "800px",
    transform: "translate(-50%, -50%) rotate(90deg)",
  });
});

test("rotationStyle: an unmeasurable frame still turns the picture", () => {
  // Before the first layout (or in a detached overlay) the frame is 0x0.
  // Falling back to the percentage box is wrong by a letterbox, but it is
  // drawable — a NaN width would blank the screen.
  for (const frame of [null, undefined, {}, { width: 0, height: 0 }, { width: "x", height: 5 }]) {
    assert.deepEqual(rotationStyle(90, frame), {
      width: "100%",
      height: "100%",
      transform: "translate(-50%, -50%) rotate(90deg)",
    });
  }
});
