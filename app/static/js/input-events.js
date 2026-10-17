// Which DOM events mean "the user touched the display" — and how to avoid
// counting one physical tap several times.
//
// THE KIOSK BROWSER EMITS A POINTER EVENT ONLY FOR THE FIRST TOUCH.
// Measured on the device on 17.10.2026 with a diagnostic build. The very
// first tap after a page load arrived as
//
//     raw touchstart / raw pointerdown / raw mousedown
//
// and was handled; every tap after it arrived as
//
//     raw touchstart / raw mousedown
//
// with no `pointerdown` at all — 16 taps in that session, 2 pointerdown
// in the whole log, and the second of those was a remote xdotool MOUSE
// click. WebKitGTK 605.1.15 under luakit evidently emulates a pointer for
// the first contact and then switches to pure touch events.
//
// Listening to `pointerdown` alone therefore meant the display reacted to
// exactly one touch per page load and ignored every one after it: the
// operating icons appeared once, then nothing — no icons, and no way back
// to the calendar either, because an unhandled touch cannot dismiss
// anything. It also explains why it was never reproducible remotely: a
// mouse click always emits `pointerdown`.
//
// Listening to all three brings the opposite hazard: ONE physical action
// arrives as two or three events, and a duplicate would be read as the
// user's SECOND tap — instantly dismissing the icons the first one just
// opened. The coalescer below drops only the derived forms.

/** Events that mean "a finger or a mouse went down on the display". */
export const TAP_EVENTS = ["touchstart", "pointerdown", "mousedown"];

/** Input that never opens the operating icons and always dismisses. */
export const DISMISS_EVENTS = ["keydown", "wheel"];

/** Everything listened to for interaction. */
export const INTERACTION_EVENTS = [...TAP_EVENTS, ...DISMISS_EVENTS];

// Comfortably above the latency of an emulated event (the classic ~300 ms
// "ghost click" plus headroom) and below any deliberate second tap.
export const COALESCE_MS = 600;

// One physical action reaches the page as two or three of these events,
// and the ORDER differs between engines: the kiosk sends touchstart
// first, Chromium sends pointerdown first. So the rule cannot rely on
// which kind arrives first. What does hold everywhere: a tap event is
// never emulated from one of its OWN kind. Two `touchstart` in a row are
// therefore two real touches, while a different kind arriving right after
// a handled one is that same tap wearing another name.

export function isTapEvent(type) {
  return TAP_EVENTS.includes(type);
}

/**
 * Returns `(type, now) => boolean`: true when the event starts a new
 * interaction, false when it is the emulated echo of the tap just
 * handled. Keyboard and wheel always pass — nothing emulates them.
 */
export function createTapCoalescer(windowMs = COALESCE_MS) {
  let lastAt = null;
  let lastType = null;
  return (type, now) => {
    if (!isTapEvent(type)) return true;
    const echo = lastAt !== null && now - lastAt < windowMs && type !== lastType;
    if (echo) return false;
    lastAt = now;
    lastType = type;
    return true;
  };
}
