// Retry state machine for the /api/config fetch at startup (Etappe 29).
//
// Boot race: after a Pi reboot the kiosk browser and the add-on start at
// the same time — the very first fetch can hit a backend that is not
// listening yet. The caller renders its built-in fallbacks immediately
// (month view, screensaver off) and this machine keeps retrying in the
// background with backoff. Contract:
// - start() runs the first attempt (awaited by the caller so the happy
//   path applies the server defaults BEFORE the first render);
// - each failure schedules the next attempt after the next delay; once
//   the delays are exhausted the machine gives up silently;
// - a success calls applyDefaults(config) ONLY while the user has not
//   interacted yet (markInteraction()); after an interaction the machine
//   stops for good — no more fetches, never a late view flip;
// - the fetched config is never persisted anywhere (only real user
//   choices go to localStorage — see view-memory.js/screensaver-memory.js).
//
// Pure logic: fetch and scheduler are injected, so the machine is
// node-testable without timers or network (tests/js/config-retry.test.mjs).

// Five retries spread over ~60 s — generous enough for the add-on to come
// up after a reboot, short enough not to poll forever.
export const RETRY_DELAYS_MS = [2000, 5000, 10000, 20000, 25000];

export function createConfigRetry({
  fetchConfig,
  applyDefaults,
  delays = RETRY_DELAYS_MS,
  schedule = (fn, delayMs) => setTimeout(fn, delayMs),
}) {
  let interacted = false;
  let settled = false;

  async function attempt(remaining) {
    if (interacted || settled) return;
    let config;
    try {
      config = await fetchConfig();
    } catch {
      const [nextDelay, ...rest] = remaining;
      if (nextDelay === undefined) {
        settled = true; // out of retries — keep the built-in fallbacks
        return;
      }
      schedule(() => attempt(rest), nextDelay);
      return;
    }
    settled = true;
    // Re-check: the user may have interacted while the fetch was in flight.
    if (!interacted) applyDefaults(config);
  }

  return {
    start: () => attempt(delays),
    markInteraction: () => {
      interacted = true;
    },
  };
}
