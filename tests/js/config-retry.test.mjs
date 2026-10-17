// Unit tests for the /api/config retry state machine (boot race after a
// Pi reboot: kiosk browser and add-on start together, the first fetch can
// fail). Contract: retries with backoff, a late success applies the server
// defaults ONLY while the user has not interacted yet, after an
// interaction the machine stops for good, and nothing is ever persisted.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createConfigRetry, RETRY_DELAYS_MS } from "../../app/static/js/config-retry.js";

const CONFIG = { default_view: "week", screensaver_default: "on" };

// Fake scheduler capturing callbacks so tests fire retries by hand.
function fakeScheduler() {
  const pending = [];
  return {
    schedule: (fn, delayMs) => pending.push({ fn, delayMs }),
    // Runs the next scheduled retry to completion (the callbacks are async).
    async runNext() {
      const next = pending.shift();
      assert.notEqual(next, undefined, "expected a scheduled retry");
      await next.fn();
    },
    pending,
  };
}

test("delays: quick at first, then patient enough to outlast a deployment", () => {
  // Two races, not one. The boot race (Pi and add-on starting together) is
  // over in seconds, so the first attempts come fast. A DEPLOYMENT is the
  // other case: `ha addons update` rebuilds the image and the add-on is
  // away for MINUTES. The old window of ~60s gave up long before it came
  // back, and the kiosk then sat there with the built-in fallbacks -
  // screensaver OFF - until somebody reloaded the page (observed on the Pi
  // on 17.10.2026: no slideshow at all after a deploy).
  assert.ok(RETRY_DELAYS_MS[0] <= 3000, "first retry must be quick");
  const total = RETRY_DELAYS_MS.reduce((sum, ms) => sum + ms, 0);
  assert.ok(total >= 480000, `total ${total}ms is too short for a deployment`);
  assert.ok(total <= 900000, `total ${total}ms would retry for ages`);
  // Monotonic backoff: never hammer a backend that is still coming up.
  for (let i = 1; i < RETRY_DELAYS_MS.length; i += 1) {
    assert.ok(RETRY_DELAYS_MS[i] >= RETRY_DELAYS_MS[i - 1], `step ${i}`);
  }
});

test("success on the first attempt applies the config, no retry scheduled", async () => {
  const applied = [];
  const scheduler = fakeScheduler();
  const retry = createConfigRetry({
    fetchConfig: async () => CONFIG,
    applyDefaults: (config) => applied.push(config),
    schedule: scheduler.schedule,
  });
  await retry.start();
  assert.deepEqual(applied, [CONFIG]);
  assert.equal(scheduler.pending.length, 0);
});

test("late success without interaction applies the config exactly once", async () => {
  let calls = 0;
  const applied = [];
  const scheduler = fakeScheduler();
  const retry = createConfigRetry({
    fetchConfig: async () => {
      calls += 1;
      if (calls <= 2) throw new Error("Verbindung fehlgeschlagen");
      return CONFIG;
    },
    applyDefaults: (config) => applied.push(config),
    delays: [2000, 5000, 10000],
    schedule: scheduler.schedule,
  });
  await retry.start();
  assert.deepEqual(applied, []); // first attempt failed, nothing applied yet
  assert.equal(scheduler.pending[0].delayMs, 2000);
  await scheduler.runNext(); // second attempt fails too
  assert.equal(scheduler.pending[0].delayMs, 5000);
  await scheduler.runNext(); // third attempt succeeds
  assert.deepEqual(applied, [CONFIG]);
  assert.equal(scheduler.pending.length, 0);
});

test("after an interaction a scheduled retry neither fetches nor applies", async () => {
  let calls = 0;
  const applied = [];
  const scheduler = fakeScheduler();
  const retry = createConfigRetry({
    fetchConfig: async () => {
      calls += 1;
      if (calls === 1) throw new Error("Verbindung fehlgeschlagen");
      return CONFIG;
    },
    applyDefaults: (config) => applied.push(config),
    schedule: scheduler.schedule,
  });
  await retry.start();
  retry.markInteraction();
  await scheduler.runNext();
  assert.equal(calls, 1); // no further fetch after the interaction
  assert.deepEqual(applied, []);
  assert.equal(scheduler.pending.length, 0);
});

test("an interaction while the fetch is in flight blocks the late apply", async () => {
  const applied = [];
  let resolveFetch;
  const scheduler = fakeScheduler();
  const retry = createConfigRetry({
    fetchConfig: () =>
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    applyDefaults: (config) => applied.push(config),
    schedule: scheduler.schedule,
  });
  const started = retry.start();
  retry.markInteraction(); // user acts while the request is still pending
  resolveFetch(CONFIG);
  await started;
  assert.deepEqual(applied, []);
});

test("gives up silently once all retries are exhausted", async () => {
  let calls = 0;
  const applied = [];
  const scheduler = fakeScheduler();
  const retry = createConfigRetry({
    fetchConfig: async () => {
      calls += 1;
      throw new Error("Verbindung fehlgeschlagen");
    },
    applyDefaults: (config) => applied.push(config),
    delays: [1000, 2000],
    schedule: scheduler.schedule,
  });
  await retry.start();
  await scheduler.runNext();
  await scheduler.runNext();
  assert.equal(calls, 3); // initial attempt + two retries
  assert.deepEqual(applied, []);
  assert.equal(scheduler.pending.length, 0);
});
