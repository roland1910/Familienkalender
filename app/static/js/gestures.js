// Horizontal swipe detection for paging on the touch display.

const MIN_HORIZONTAL_DISTANCE = 60; // px
const MAX_VERTICAL_DRIFT = 80; // px — anything steeper is a scroll

export function attachSwipe(element, { onSwipeLeft, onSwipeRight }) {
  let startX = null;
  let startY = null;

  element.addEventListener(
    "touchstart",
    (touchEvent) => {
      if (touchEvent.touches.length !== 1) {
        startX = null;
        return;
      }
      startX = touchEvent.touches[0].clientX;
      startY = touchEvent.touches[0].clientY;
    },
    { passive: true },
  );

  element.addEventListener(
    "touchend",
    (touchEvent) => {
      if (startX === null) return;
      const touch = touchEvent.changedTouches[0];
      const deltaX = touch.clientX - startX;
      const deltaY = touch.clientY - startY;
      startX = null;
      if (Math.abs(deltaX) < MIN_HORIZONTAL_DISTANCE) return;
      if (Math.abs(deltaY) > MAX_VERTICAL_DRIFT) return;
      if (deltaX < 0) {
        onSwipeLeft();
      } else {
        onSwipeRight();
      }
    },
    { passive: true },
  );
}
