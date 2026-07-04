// Tests for the pure power-view formatting/classification helpers.

import assert from "node:assert/strict";
import { test } from "node:test";

import { balanceTile, formatWatts } from "../../app/static/js/power-format.js";

test("formatWatts rounds and uses German thousands separators", () => {
  assert.equal(formatWatts(0), "0 W");
  assert.equal(formatWatts(45.3), "45 W");
  assert.equal(formatWatts(1234.5), "1.235 W");
  assert.equal(formatWatts(-136.7), "-137 W");
});

test("balanceTile shows a green surplus when the PV covers the load", () => {
  const tile = balanceTile({ value: 120.5, available: true }, { value: 0, available: true });
  assert.equal(tile.state, "surplus");
  assert.equal(tile.label, "Überschuss");
  assert.equal(tile.value, 120.5);
  assert.equal(tile.available, true);
});

test("balanceTile shows a red grid import when power is drawn", () => {
  const tile = balanceTile({ value: 0, available: true }, { value: 136.7, available: true });
  assert.equal(tile.state, "grid");
  assert.equal(tile.label, "Netzbezug");
  assert.equal(tile.value, 136.7);
});

test("balanceTile is neutral when production exactly covers the load", () => {
  const tile = balanceTile({ value: 0, available: true }, { value: 0, available: true });
  assert.equal(tile.state, "balanced");
  assert.equal(tile.label, "Ausgeglichen");
  assert.equal(tile.value, 0);
});

test("balanceTile flags both sensors being unavailable", () => {
  const tile = balanceTile({ value: 0, available: false }, { value: 0, available: false });
  assert.equal(tile.state, "balanced");
  assert.equal(tile.label, "Bilanz");
  assert.equal(tile.value, 0);
  assert.equal(tile.available, false);
});

test("balanceTile trusts the remaining sensor when only one is unavailable", () => {
  const tile = balanceTile({ value: 80, available: true }, { value: 0, available: false });
  assert.equal(tile.state, "surplus");
  assert.equal(tile.available, true);
});
