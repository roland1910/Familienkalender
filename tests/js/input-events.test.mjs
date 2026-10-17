// Unit tests for the interaction-event rules.
//
// The case that matters is the real one from the kiosk (17.10.2026): the
// first touch after a page load arrives as touchstart + pointerdown +
// mousedown, every later touch only as touchstart + mousedown. Both must
// count as exactly ONE interaction each.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  COALESCE_MS,
  createTapCoalescer,
  DISMISS_EVENTS,
  INTERACTION_EVENTS,
  isTapEvent,
  TAP_EVENTS,
} from "../../app/static/js/input-events.js";

test("touch is listened to, not only pointer events", () => {
  // The whole point: a finger on the kiosk never produces pointerdown
  // after the first tap, so touchstart must be in the list.
  assert.ok(TAP_EVENTS.includes("touchstart"));
  assert.ok(TAP_EVENTS.includes("pointerdown"));
  assert.ok(TAP_EVENTS.includes("mousedown"));
  assert.deepEqual(DISMISS_EVENTS, ["keydown", "wheel"]);
  assert.equal(INTERACTION_EVENTS.length, TAP_EVENTS.length + DISMISS_EVENTS.length);
  assert.ok(isTapEvent("touchstart"));
  assert.ok(!isTapEvent("keydown"));
});

test("the kiosk's first tap (touch+pointer+mouse) counts once", () => {
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("pointerdown", 1010), false);
  assert.equal(fresh("mousedown", 1050), false);
});

test("the ORDER of the three does not matter", () => {
  // Chromium emits pointerdown before touchstart, the kiosk the other way
  // round. A rule that assumed one order counted a Playwright touch tap
  // twice and dismissed the icons it had just opened.
  const chromium = createTapCoalescer();
  assert.equal(chromium("pointerdown", 1000), true);
  assert.equal(chromium("touchstart", 1005), false);
  assert.equal(chromium("mousedown", 1060), false);
});

test("the kiosk's later taps (touch+mouse) count once each", () => {
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("mousedown", 1040), false);
  // Second physical tap, well after the window.
  assert.equal(fresh("touchstart", 3000), true);
  assert.equal(fresh("mousedown", 3040), false);
});

test("two real touches in quick succession both count", () => {
  // Decisive: the second tap is how the user leaves the slideshow. It must
  // never be mistaken for the emulated echo of the first, even when it
  // lands inside the coalescing window.
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("touchstart", 1200), true);
  // The same for a mouse clicked twice quickly.
  const mouse = createTapCoalescer();
  assert.equal(mouse("pointerdown", 1000), true);
  assert.equal(mouse("pointerdown", 1200), true);
});

test("a second touch right after the first one's echo still counts", () => {
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("mousedown", 1040), false);
  // The echo must not extend the window: the next real touch is a tap.
  assert.equal(fresh("touchstart", 1100), true);
});

test("after the window everything counts again", () => {
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("mousedown", 1000 + COALESCE_MS), true);
});

test("keyboard and wheel are never coalesced away", () => {
  const fresh = createTapCoalescer();
  assert.equal(fresh("touchstart", 1000), true);
  assert.equal(fresh("keydown", 1010), true);
  assert.equal(fresh("wheel", 1020), true);
});
