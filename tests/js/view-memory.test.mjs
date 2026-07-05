// Unit tests for the per-device view persistence (localStorage): the
// serialized state must round-trip, and broken or foreign localStorage
// values must fall back to null (defaults) instead of crashing.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deserializeViewState,
  loadViewState,
  saveViewState,
  serializeViewState,
  STORAGE_KEY,
} from "../../app/static/js/view-memory.js";

function roundtrip(state) {
  return deserializeViewState(serializeViewState(state));
}

test("roundtrip: week view with anchor date and calendar mode", () => {
  const state = { view: "week", anchor: new Date(2026, 6, 8), mode: "calendar" };
  const restored = roundtrip(state);
  assert.equal(restored.view, "week");
  assert.equal(restored.mode, "calendar");
  assert.equal(restored.anchor.getTime(), new Date(2026, 6, 8).getTime());
});

test("roundtrip: month view with power mode", () => {
  const state = { view: "month", anchor: new Date(2025, 11, 31), mode: "power" };
  const restored = roundtrip(state);
  assert.equal(restored.view, "month");
  assert.equal(restored.mode, "power");
  assert.equal(restored.anchor.getTime(), new Date(2025, 11, 31).getTime());
});

test("serialize: anchor is stored as a plain ISO date", () => {
  const raw = serializeViewState({
    view: "week",
    anchor: new Date(2026, 0, 5, 14, 30), // time of day must not leak in
    mode: "calendar",
  });
  assert.deepEqual(JSON.parse(raw), { view: "week", anchor: "2026-01-05", mode: "calendar" });
});

test("deserialize: garbage inputs fall back to null", () => {
  const garbage = [
    null,
    undefined,
    "",
    "not json",
    "42",
    '"a string"',
    "[]",
    "{}",
    '{"view":"week"}',
    '{"view":"day","anchor":"2026-07-08","mode":"calendar"}',
    '{"view":"week","anchor":"2026-07-08","mode":"disco"}',
    '{"view":"week","anchor":12345,"mode":"calendar"}',
  ];
  for (const raw of garbage) {
    assert.equal(deserializeViewState(raw), null, `expected null for ${JSON.stringify(raw)}`);
  }
});

test("deserialize: invalid dates fall back to null", () => {
  const badDates = [
    "2026-02-31", // impossible day (would silently roll over)
    "2026-13-01", // impossible month
    "07/08/2026",
    "2026-7-8", // not zero-padded ISO
    "2026-07-08T12:00:00", // date-times are not accepted
    "yesterday",
  ];
  for (const anchor of badDates) {
    const raw = JSON.stringify({ view: "week", anchor, mode: "calendar" });
    assert.equal(deserializeViewState(raw), null, `expected null for anchor ${anchor}`);
  }
});

test("loadViewState: reads and validates the stored value", () => {
  const storage = new Map([[STORAGE_KEY, '{"view":"week","anchor":"2026-07-06","mode":"power"}']]);
  const fake = { getItem: (key) => storage.get(key) ?? null };
  const restored = loadViewState(fake);
  assert.equal(restored.view, "week");
  assert.equal(restored.mode, "power");
  assert.equal(restored.anchor.getTime(), new Date(2026, 6, 6).getTime());
});

test("loadViewState: missing key falls back to null", () => {
  assert.equal(loadViewState({ getItem: () => null }), null);
});

test("loadViewState: a throwing storage falls back to null (no crash)", () => {
  const fake = {
    getItem: () => {
      throw new Error("storage disabled");
    },
  };
  assert.equal(loadViewState(fake), null);
});

test("loadViewState: unavailable storage (undefined) falls back to null", () => {
  assert.equal(loadViewState(undefined), null);
});

test("saveViewState: writes the serialized state under the storage key", () => {
  const written = {};
  const fake = {
    setItem: (key, value) => {
      written[key] = value;
    },
  };
  saveViewState({ view: "month", anchor: new Date(2026, 6, 5), mode: "calendar" }, fake);
  assert.deepEqual(JSON.parse(written[STORAGE_KEY]), {
    view: "month",
    anchor: "2026-07-05",
    mode: "calendar",
  });
});

test("saveViewState: a throwing storage is ignored (best effort, no crash)", () => {
  const fake = {
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
  saveViewState({ view: "week", anchor: new Date(2026, 6, 5), mode: "calendar" }, fake);
});

test("saveViewState: unavailable storage (undefined) is ignored", () => {
  saveViewState({ view: "week", anchor: new Date(2026, 6, 5), mode: "calendar" }, undefined);
});
