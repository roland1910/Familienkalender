// Tests for the pure device-list text format of the admin settings
// (one line per device: "entity_id = Anzeigename").

import assert from "node:assert/strict";
import { test } from "node:test";

import {
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

test("parseDeviceLines reports a German error with the line number", () => {
  const result = parseDeviceLines("sensor.ok = Gut\nnur-eine-id");
  assert.equal(result.devices, null);
  assert.match(result.error, /Zeile 2/);
  assert.match(result.error, /entity_id = Anzeigename/);
});

test("parseDeviceLines rejects lines with an empty name or id", () => {
  assert.match(parseDeviceLines("sensor.a =").error, /Zeile 1/);
  assert.match(parseDeviceLines("= Nur Name").error, /Zeile 1/);
});

test("formatDeviceLines renders one line per device", () => {
  const text = formatDeviceLines([
    { entity_id: "sensor.kuhlschrank_leistung", name: "Kühlschrank" },
    { entity_id: "sensor.tv_leistung", name: "TV" },
  ]);
  assert.equal(text, "sensor.kuhlschrank_leistung = Kühlschrank\nsensor.tv_leistung = TV");
});

test("format and parse are inverse for well-formed lists", () => {
  const devices = [{ entity_id: "sensor.a_leistung", name: "Gerät A" }];
  assert.deepEqual(parseDeviceLines(formatDeviceLines(devices)).devices, devices);
});
