// Tests for the slideshow's "Bild zurück" trail (17.11.2026).
//
// The behaviour worth nailing down is not "remember the last picture" but
// what happens AROUND stepping back: waiting after a step back must replay
// forward rather than jumping to a new photo, a photo marked for deletion
// must never come back, and the trail must survive the slideshow being
// stopped and resumed (the module keeps no reference to the view, so that
// last one is really a statement about who owns the state — asserted here
// by using one trail across two "sessions").

import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_TRAIL, createTrail } from "../../app/static/js/slideshow-trail.js";

const photo = (id) => ({ id, name: `${id}.jpg`, rotation: 0 });

test("a fresh trail has nothing to step back to", () => {
  const trail = createTrail();
  assert.equal(trail.canGoBack(), false);
  assert.equal(trail.back(), null);
  assert.equal(trail.current(), null);
});

test("one picture is not enough to step back", () => {
  const trail = createTrail();
  trail.record(photo(1));
  assert.equal(trail.canGoBack(), false);
  assert.equal(trail.back(), null);
  assert.deepEqual(trail.current(), photo(1));
});

test("back walks to the previous picture, and again", () => {
  const trail = createTrail();
  [1, 2, 3].forEach((id) => trail.record(photo(id)));
  assert.equal(trail.back().id, 2);
  assert.equal(trail.back().id, 1);
  assert.equal(trail.canGoBack(), false);
  assert.equal(trail.back(), null, "never walks off the front");
});

test("the automatic advance replays forward instead of drawing a new one", () => {
  // THE point of the cursor: stepping back and then simply waiting must
  // show the picture that was already on its way, not a surprise one.
  const trail = createTrail();
  [1, 2, 3].forEach((id) => trail.record(photo(id)));
  trail.back();
  trail.back(); // now on 1
  assert.equal(trail.replayNext().id, 2);
  assert.equal(trail.replayNext().id, 3);
  assert.equal(trail.replayNext(), null, "at the end the caller draws a fresh one");
});

test("a fresh draw after replaying to the end extends the trail", () => {
  const trail = createTrail();
  trail.record(photo(1));
  trail.back();
  assert.equal(trail.replayNext(), null);
  trail.record(photo(2));
  assert.equal(trail.size(), 2);
  assert.equal(trail.back().id, 1);
});

test("a picture marked for deletion never comes back", () => {
  const trail = createTrail();
  [1, 2, 3].forEach((id) => trail.record(photo(id)));
  trail.forget(3); // the one on screen, just marked
  assert.equal(trail.current().id, 2);
  assert.equal(trail.back().id, 1);
  assert.equal(trail.size(), 2);
});

test("forgetting an earlier picture keeps the cursor on the current one", () => {
  const trail = createTrail();
  [1, 2, 3].forEach((id) => trail.record(photo(id)));
  trail.forget(1);
  assert.equal(trail.current().id, 3);
  assert.equal(trail.back().id, 2);
  assert.equal(trail.back(), null);
});

test("forgetting a picture that was shown twice drops both copies", () => {
  const trail = createTrail();
  [1, 2, 1].forEach((id) => trail.record(photo(id)));
  trail.forget(1);
  assert.equal(trail.size(), 1);
  assert.equal(trail.current().id, 2);
});

test("forgetting an unknown id changes nothing", () => {
  const trail = createTrail();
  [1, 2].forEach((id) => trail.record(photo(id)));
  trail.forget(99);
  trail.forget(null);
  trail.forget(undefined);
  assert.equal(trail.size(), 2);
  assert.equal(trail.current().id, 2);
});

test("the trail is capped and drops the oldest entries", () => {
  const trail = createTrail(3);
  [1, 2, 3, 4, 5].forEach((id) => trail.record(photo(id)));
  assert.equal(trail.size(), 3);
  assert.equal(trail.current().id, 5);
  assert.equal(trail.back().id, 4);
  assert.equal(trail.back().id, 3);
  assert.equal(trail.canGoBack(), false, "1 and 2 fell off the front");
});

test("an unusable limit falls back to the default rather than losing everything", () => {
  // A zero or negative cap would make `record` a no-op and the button dead.
  for (const limit of [0, -5, Number.NaN, "viele"]) {
    const trail = createTrail(limit);
    trail.record(photo(1));
    trail.record(photo(2));
    assert.equal(trail.size(), 2, String(limit));
  }
  assert.equal(MAX_TRAIL > 1, true);
});

test("nothing is recorded for a missing item", () => {
  const trail = createTrail();
  trail.record(null);
  trail.record(undefined);
  assert.equal(trail.size(), 0);
  assert.equal(trail.current(), null);
});

test("the trail carries across a stop and resume of the slideshow", () => {
  // The module holds no reference to the view: the same trail object simply
  // keeps its items, which is what lets stopSlideshow leave it alone.
  const trail = createTrail();
  [1, 2].forEach((id) => trail.record(photo(id)));
  // ... viewer taps into the calendar, later presses play; the slideshow
  // draws a fresh picture and the trail continues where it left off.
  assert.equal(trail.replayNext(), null);
  trail.record(photo(3));
  assert.equal(trail.back().id, 2);
});

test("a rotation applied to the item on screen is remembered with it", () => {
  // The view stores the very object it got from the server, so turning the
  // picture and stepping back to it later must show it turned.
  const trail = createTrail();
  trail.record(photo(1));
  trail.record(photo(2));
  trail.current().rotation = 90;
  trail.back();
  assert.equal(trail.replayNext().rotation, 90);
});
