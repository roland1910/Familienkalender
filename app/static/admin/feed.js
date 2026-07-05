// Feed subscription section: shows the https subscription URL (with the
// URL token), lets the admin set the public host for it and rotates the
// token via a two-step confirmation button.

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";

const ROTATE_LABEL = "Neuen Link erzeugen";

function applyFeed(feed) {
  // Prefer the absolute URL (configured public host, else the request
  // host); fall back to the bare path.
  byId("feed-url").value = feed.url || feed.path;
  byId("feed-host").value = feed.public_host || "";
}

export async function loadFeed() {
  const { feed } = await api.getFeed();
  applyFeed(feed);
}

export function initFeed() {
  byId("btn-feed-host-save").addEventListener("click", async () => {
    const messageNode = byId("feed-message");
    try {
      const { feed } = await api.saveFeedHost(byId("feed-host").value.trim());
      applyFeed(feed);
      showMessage(messageNode, "Host gespeichert — die Abo-Adresse ist aktualisiert.");
    } catch (error) {
      showMessage(messageNode, error.message, true);
    }
  });

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
