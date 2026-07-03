// Settings section: evening boundary for the filtered display mode.

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";

export async function loadSettings() {
  const settings = await api.getSettings();
  byId("evening-boundary").value = settings.evening_boundary;
}

export function initSettings() {
  byId("btn-save-settings").addEventListener("click", async () => {
    const messageNode = byId("settings-message");
    try {
      await api.saveSettings(byId("evening-boundary").value);
      showMessage(messageNode, "Gespeichert.");
    } catch (error) {
      showMessage(messageNode, error.message, true);
    }
  });
}
