// Pure text format for the power-view device list in the settings
// section: one line per device, "entity_id = Anzeigename". The "= name"
// is optional — a line with just an entity_id uses the sensor's HA
// friendly_name as the display name. Extracted so the parsing is
// unit-testable with plain node --test (no DOM).

/**
 * Parse the textarea content into a device list.
 *
 * Returns `{devices, error: null}` on success (blank lines are skipped,
 * everything is trimmed; the first "=" separates id and name, further
 * "=" stay part of the name). A line with no "=" is a bare entity_id and
 * yields an empty name (the HA friendly_name is used at display time).
 * On a malformed line returns `{devices: null, error}` with a German
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
    // No "=" → bare entity_id, name stays empty (use the HA friendly_name).
    const entityId = (separator === -1 ? line : line.slice(0, separator)).trim();
    const name = separator === -1 ? "" : line.slice(separator + 1).trim();
    if (entityId === "") {
      return {
        devices: null,
        error:
          `Zeile ${lineNumber}: Die entity_id darf nicht leer sein` +
          ` — Format "entity_id" oder "entity_id = Anzeigename".`,
      };
    }
    devices.push({ entity_id: entityId, name });
  }
  return { devices, error: null };
}

/**
 * The name to show for a device, in priority order:
 *   1. the manually configured override (power_devices name), if set,
 *   2. otherwise the sensor's HA friendly_name,
 *   3. otherwise the raw entity_id (last-resort fallback).
 * Each candidate is trimmed; blank ones are skipped.
 */
export function deviceDisplayName(configuredName, friendlyName, entityId) {
  for (const candidate of [configuredName, friendlyName, entityId]) {
    const trimmed = (candidate ?? "").trim();
    if (trimmed !== "") return trimmed;
  }
  return "";
}

/** The device list as textarea content (inverse of parseDeviceLines).
 *
 * A device with an empty name is written as a bare entity_id, so the
 * "use the HA friendly_name" choice round-trips through the textarea. */
export function formatDeviceLines(devices) {
  return devices
    .map((device) => (device.name ? `${device.entity_id} = ${device.name}` : device.entity_id))
    .join("\n");
}
