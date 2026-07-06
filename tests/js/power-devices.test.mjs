// Tests for the pure device-list text format of the admin settings
// (one line per device: "entity_id = Anzeigename").

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deviceDisplayName,
  formatDeviceLines,
  parseDeviceLines,
} from "../../app/static/admin/power-devices.js";

test("parseDeviceLines parses one device per line", () => {
  const result = parseDeviceLines(
    "sensor.kuhlschrank_leistung = Kühlschrank\nsensor.tv_leistung = TV",
  );
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, [
    { entity_id: "sensor.kuhlschrank_leistung", name: "Kühlschrank" },
    { entity_id: "sensor.tv_leistung", name: "TV" },
  ]);
});

test("parseDeviceLines skips blank lines and trims whitespace", () => {
  const result = parseDeviceLines("\n  sensor.a_leistung=Gerät A  \n\n");
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, [{ entity_id: "sensor.a_leistung", name: "Gerät A" }]);
});

test("parseDeviceLines keeps extra equals signs in the display name", () => {
  const result = parseDeviceLines("sensor.a = Name = mit Gleichheitszeichen");
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, [
    { entity_id: "sensor.a", name: "Name = mit Gleichheitszeichen" },
  ]);
});

test("parseDeviceLines returns an empty list for empty text", () => {
  const result = parseDeviceLines("   \n  ");
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, []);
});

test("parseDeviceLines accepts a bare entity_id (no '=') with an empty name", () => {
  // A line with just an entity_id means "use the HA friendly_name".
  const result = parseDeviceLines("sensor.ok = Gut\nsensor.nur_id");
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, [
    { entity_id: "sensor.ok", name: "Gut" },
    { entity_id: "sensor.nur_id", name: "" },
  ]);
});

test("parseDeviceLines treats 'entity_id =' (empty name) as a bare id", () => {
  const result = parseDeviceLines("sensor.a =");
  assert.equal(result.error, null);
  assert.deepEqual(result.devices, [{ entity_id: "sensor.a", name: "" }]);
});

test("parseDeviceLines rejects a line whose entity_id is empty", () => {
  for (const line of ["= Nur Name", "   =   "]) {
    const result = parseDeviceLines(line);
    assert.equal(result.devices, null);
    assert.match(result.error, /Zeile 1/);
    assert.match(result.error, /entity_id/);
    assert.match(result.error, /nicht leer/);
  }
});

test("formatDeviceLines renders one line per device", () => {
  const text = formatDeviceLines([
    { entity_id: "sensor.kuhlschrank_leistung", name: "Kühlschrank" },
    { entity_id: "sensor.tv_leistung", name: "TV" },
  ]);
  assert.equal(text, "sensor.kuhlschrank_leistung = Kühlschrank\nsensor.tv_leistung = TV");
});

test("formatDeviceLines writes a name-less device as a bare entity_id", () => {
  const text = formatDeviceLines([
    { entity_id: "sensor.a_leistung", name: "Gerät A" },
    { entity_id: "sensor.b_leistung", name: "" },
  ]);
  assert.equal(text, "sensor.a_leistung = Gerät A\nsensor.b_leistung");
});

test("format and parse are inverse for well-formed lists", () => {
  const devices = [
    { entity_id: "sensor.a_leistung", name: "Gerät A" },
    { entity_id: "sensor.b_leistung", name: "" },
  ];
  assert.deepEqual(parseDeviceLines(formatDeviceLines(devices)).devices, devices);
});

test("deviceDisplayName prefers the configured override", () => {
  assert.equal(deviceDisplayName("Mein Name", "HA Name", "sensor.x"), "Mein Name");
});

test("deviceDisplayName falls back to the HA friendly_name when no override", () => {
  assert.equal(deviceDisplayName("", "HA Name", "sensor.x"), "HA Name");
  assert.equal(deviceDisplayName("   ", "HA Name", "sensor.x"), "HA Name");
  assert.equal(deviceDisplayName(null, "HA Name", "sensor.x"), "HA Name");
});

test("deviceDisplayName falls back to the entity_id as a last resort", () => {
  assert.equal(deviceDisplayName("", "", "sensor.x"), "sensor.x");
  assert.equal(deviceDisplayName("", null, "sensor.x"), "sensor.x");
});
