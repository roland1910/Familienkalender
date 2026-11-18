// The slideshow's trail of recently shown items — what the "Bild zurück"
// button walks back along. DOM-free and stateful, so it can be exercised
// with plain `node --test` (pattern: createConfigRetry, createTapCoalescer).
//
// WHY A CURSOR AND NOT JUST A STACK: stepping back and then waiting must do
// something predictable. With a cursor, the automatic advance walks FORWARD
// along the same trail and only draws a fresh picture once it runs off the
// end — so going back one and waiting shows the picture that was already on
// its way, exactly like a browser's back button. A plain stack would jump to
// a brand-new photo instead, and the viewer would have lost the one they
// just stepped back to see.
//
// THE TRAIL OUTLIVES THE SLIDESHOW, deliberately (Roland, 17.11.2026): "dies
// soll auch funktionieren wenn man aus einem bild in den kalender gewechselt
// hat und dann über den play button die diashow fortsetzt". A viewer who
// taps back into the calendar and resumes is in the middle of one viewing
// session as far as they are concerned, so `stopSlideshow` must not clear
// this. It is per page load; a kiosk restart starts a fresh trail.

/**
 * How many items to remember. A trail of 50 is some 25 minutes of viewing
 * at 30s a picture — far past what anyone steps back through — while the
 * items themselves are small metadata objects, so the cap is about never
 * growing without bound over days of continuous running, not about memory
 * pressure today.
 */
export const MAX_TRAIL = 50;

/**
 * A trail with a cursor on the item currently on screen.
 *
 * `record` appends (and is only ever called once the trail has been replayed
 * to its end), `replayNext` walks forward, `back` walks backward, and
 * `forget` drops an item entirely — used when a photo is marked for
 * deletion, because the backend stops handing it out and stepping back onto
 * it would contradict that.
 */
export function createTrail(limit = MAX_TRAIL) {
  const cap = Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : MAX_TRAIL;
  let items = [];
  let index = -1;

  function trim() {
    const excess = items.length - cap;
    if (excess <= 0) return;
    items = items.slice(excess);
    index = Math.max(-1, index - excess);
  }

  return {
    /** Append a freshly drawn item and make it the current one. */
    record(media) {
      if (media === null || media === undefined) return;
      items.push(media);
      index = items.length - 1;
      trim();
    },

    /**
     * The next item already on the trail (after a step back), or null when
     * the cursor is at the end and the caller must draw a fresh one.
     */
    replayNext() {
      if (index + 1 >= items.length) return null;
      index += 1;
      return items[index];
    },

    /** Whether there is an earlier item to step back to. */
    canGoBack() {
      return index > 0;
    },

    /** Step back one item and return it, or null if there is none. */
    back() {
      if (index <= 0) return null;
      index -= 1;
      return items[index];
    },

    /** The item on screen, or null before the first one. */
    current() {
      return index >= 0 && index < items.length ? items[index] : null;
    },

    /**
     * Drop every item with this id. The cursor keeps pointing at the same
     * item it did before where possible; when the current item itself is
     * dropped, it lands on the one in front of it, so a following `back`
     * never hands the dropped item out again.
     */
    forget(id) {
      if (id === null || id === undefined) return;
      let removedAtOrBefore = 0;
      const kept = [];
      items.forEach((item, position) => {
        if (item?.id === id) {
          if (position <= index) removedAtOrBefore += 1;
          return;
        }
        kept.push(item);
      });
      if (removedAtOrBefore === 0 && kept.length === items.length) return;
      items = kept;
      index = Math.min(items.length - 1, Math.max(-1, index - removedAtOrBefore));
    },

    /** How many items are remembered (for tests and diagnostics). */
    size() {
      return items.length;
    },
  };
}
