// Settings section: evening boundary for the filtered display mode and
// the device list of the power view.

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";
import { formatDeviceLines, parseDeviceLines } from "./power-devices.js";

export async function loadSettings() {
  const settings = await api.getSettings();
  byId("evening-boundary").value = settings.evening_boundary;
  byId("power-devices").value = formatDeviceLines(settings.power_devices);
}

function initPowerDevices() {
  byId("btn-save-power-devices").addEventListener("click", async () => {
    const messageNode = byId("power-devices-message");
    const { devices, error } = parseDeviceLines(byId("power-devices").value);
    if (error !== null) {
      showMessage(messageNode, error, true);
      return;
    }
    try {
      const settings = await api.savePowerDevices(devices);
      // Re-render normalized (trimmed, canonical separator spacing).
      byId("power-devices").value = formatDeviceLines(settings.power_devices);
      showMessage(messageNode, "Gespeichert.");
    } catch (saveError) {
      showMessage(messageNode, saveError.message, true);
    }
  });
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
  initPowerDevices();
}
