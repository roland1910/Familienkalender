// Pure text format for the power-view device list in the settings
// section: one line per device, "entity_id = Anzeigename". Extracted so
// the parsing is unit-testable with plain node --test (no DOM).

/**
 * Parse the textarea content into a device list.
 *
 * Returns `{devices, error: null}` on success (blank lines are skipped,
 * everything is trimmed; the first "=" separates id and name, further
 * "=" stay part of the name) or `{devices: null, error}` with a German
 * message naming the offending line.
 */
export function parseDeviceLines(text) {
  const devices = [];
  const lines = text.split("\n");
  for (const [index, rawLine] of lines.entries()) {
    const line = rawLine.trim();
    if (line === "") continue;
    const lineNumber = index + 1;
    const separator = line.indexOf("=");
    if (separator === -1) {
      return {
        devices: null,
        error:
          `Zeile ${lineNumber}: Kein "=" gefunden — bitte im Format` +
          ` "entity_id = Anzeigename" angeben.`,
      };
    }
    const entityId = line.slice(0, separator).trim();
    const name = line.slice(separator + 1).trim();
    if (entityId === "" || name === "") {
      return {
        devices: null,
        error:
          `Zeile ${lineNumber}: entity_id und Anzeigename dürfen nach dem` +
          ` Trennen am "=" nicht leer sein.`,
      };
    }
    devices.push({ entity_id: entityId, name });
  }
  return { devices, error: null };
}

/** The device list as textarea content (inverse of parseDeviceLines). */
export function formatDeviceLines(devices) {
  return devices.map((device) => `${device.entity_id} = ${device.name}`).join("\n");
}
