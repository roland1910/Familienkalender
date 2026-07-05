// Feed subscription section: shows the subscription URL (with the URL
// token) and rotates the token via a two-step confirmation button.

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";

const ROTATE_LABEL = "Neuen Link erzeugen";

function applyFeed(feed) {
  // Prefer the best-effort absolute URL; fall back to the bare path
  // (the hint below the field explains how to complete it).
  byId("feed-url").value = feed.url || feed.path;
}

export async function loadFeed() {
  const { feed } = await api.getFeed();
  applyFeed(feed);
}

export function initFeed() {
  const button = byId("btn-feed-rotate");
  let armed = false;
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Wirklich? Das bisherige Abo wird ungültig";
      setTimeout(() => {
        armed = false;
        button.textContent = ROTATE_LABEL;
      }, 5000);
      return;
    }
    armed = false;
    button.textContent = ROTATE_LABEL;
    const messageNode = byId("feed-message");
    try {
      const { feed } = await api.rotateFeed();
      applyFeed(feed);
      showMessage(messageNode, "Neuer Abo-Link erzeugt — der alte Link ist ab sofort ungültig.");
    } catch (error) {
      showMessage(messageNode, error.message, true);
    }
  });
}
