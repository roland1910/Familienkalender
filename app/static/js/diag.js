// TEMPORARY diagnostics (17.10.2026) — remove once the cause is found.
//
// The kiosk stops acting on input after a while (no icons, no calendar)
// while the slideshow keeps advancing; mouse clicks fail exactly like
// touches, so it is not a touch problem. See app/diag.py for why no other
// channel works here (no dev tools, broken screenshot endpoint, and
// document.title never reaches the window because of the Ingress iframe).
//
// What this answers, in order of importance:
//  1. Do the interactions reach the page at all? An OWN capture listener
//     on window records them independently of markInteraction, so "no
//     event arrived" can be told apart from "the handler misbehaved".
//  2. Did anything throw? A single uncaught error that skips the exit path
//     is the prime suspect, and errors are invisible without dev tools.
//  3. Is the page still alive while it is deaf? The heartbeat separates
//     "stopped reporting" from "reporting, but seeing no input".

const HEARTBEAT_MS = 60000;

export function diag(line) {
  try {
    fetch("api/_diag", { method: "POST", body: String(line), keepalive: true }).catch(() => {});
  } catch {
    // A diagnostic must never break the page it observes.
  }
}

function overlayCount() {
  try {
    return document.querySelectorAll(".slideshow-overlay").length;
  } catch {
    return -1;
  }
}

/** Install the independent listeners and the heartbeat. Call once. */
export function installDiagnostics(describeState = () => "") {
  diag(`boot ua=${navigator.userAgent.slice(0, 60)}`);

  // Deliberately separate from markInteraction: this one only records.
  for (const type of ["pointerdown", "touchstart", "mousedown", "keydown", "wheel"]) {
    window.addEventListener(
      type,
      (event) => {
        const target = event.target;
        const tag = target instanceof Element ? target.tagName : "?";
        const cls = target instanceof Element ? String(target.className).slice(0, 40) : "";
        diag(`raw ${type} tag=${tag} cls=${cls} ov=${overlayCount()}`);
      },
      { capture: true, passive: true },
    );
  }

  window.addEventListener("error", (event) => {
    diag(
      `ERROR ${event.message} @${event.filename}:${event.lineno}:${event.colno}` +
        ` ${event.error?.stack ?? ""}`,
    );
  });
  window.addEventListener("unhandledrejection", (event) => {
    diag(`REJECT ${event.reason?.stack ?? event.reason}`);
  });
  window.addEventListener("visibilitychange", () => {
    diag(`visibility ${document.visibilityState}`);
  });

  setInterval(() => {
    diag(`beat ov=${overlayCount()} ${describeState()}`);
  }, HEARTBEAT_MS);
}
