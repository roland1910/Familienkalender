// Unit tests for the per-device screensaver toggle persistence and the
// priority resolution against the server default (Etappe 29): an explicit
// device choice always wins, else the server default, else OFF. A
// throwing/undefined storage must never crash the app.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  loadScreensaverChoice,
  resolveScreensaverEnabled,
  saveScreensaverEnabled,
  STORAGE_KEY,
} from "../../app/static/js/screensaver-memory.js";

test("choice: missing value reads as null (no device choice)", () => {
  assert.equal(loadScreensaverChoice({ getItem: () => null }), null);
});

test("choice: '1' reads as ON, '0' as OFF, anything else as null", () => {
  assert.equal(loadScreensaverChoice({ getItem: () => "1" }), true);
  assert.equal(loadScreensaverChoice({ getItem: () => "0" }), false);
  assert.equal(loadScreensaverChoice({ getItem: () => "true" }), null);
  assert.equal(loadScreensaverChoice({ getItem: () => "yes" }), null);
  assert.equal(loadScreensaverChoice({ getItem: () => "" }), null);
});

test("choice: a throwing storage reads as null (no crash)", () => {
  const fake = {
    getItem: () => {
      throw new Error("storage disabled");
    },
  };
  assert.equal(loadScreensaverChoice(fake), null);
});

test("choice: unavailable storage (undefined) reads as null", () => {
  assert.equal(loadScreensaverChoice(undefined), null);
});

test("resolve: an explicit device choice always wins over the server default", () => {
  assert.equal(resolveScreensaverEnabled(true, "off"), true);
  assert.equal(resolveScreensaverEnabled(false, "on"), false);
  assert.equal(resolveScreensaverEnabled(true, "on"), true);
  assert.equal(resolveScreensaverEnabled(false, "off"), false);
});

test("resolve: without a device choice the server default decides", () => {
  assert.equal(resolveScreensaverEnabled(null, "on"), true);
  assert.equal(resolveScreensaverEnabled(null, "off"), false);
});

test("resolve: without any input the screensaver stays OFF", () => {
  assert.equal(resolveScreensaverEnabled(null, null), false);
  assert.equal(resolveScreensaverEnabled(null, undefined), false);
});

test("resolve: an unknown server value is treated as OFF (untrusted payload)", () => {
  assert.equal(resolveScreensaverEnabled(null, "ON"), false);
  assert.equal(resolveScreensaverEnabled(null, "1"), false);
  assert.equal(resolveScreensaverEnabled(null, "disco"), false);
});

test("save: ON writes '1', OFF writes '0'", () => {
  const written = {};
  const fake = {
    setItem: (key, value) => {
      written[key] = value;
    },
  };
  saveScreensaverEnabled(true, fake);
  assert.equal(written[STORAGE_KEY], "1");
  saveScreensaverEnabled(false, fake);
  assert.equal(written[STORAGE_KEY], "0");
});

test("save: round-trips through the same fake storage", () => {
  const store = new Map();
  const fake = {
    getItem: (key) => store.get(key) ?? null,
    setItem: (key, value) => store.set(key, value),
  };
  saveScreensaverEnabled(true, fake);
  assert.equal(loadScreensaverChoice(fake), true);
  saveScreensaverEnabled(false, fake);
  assert.equal(loadScreensaverChoice(fake), false);
});

test("save: a throwing storage is ignored (best effort, no crash)", () => {
  const fake = {
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
  saveScreensaverEnabled(true, fake);
});

test("save: unavailable storage (undefined) is ignored", () => {
  saveScreensaverEnabled(true, undefined);
});
