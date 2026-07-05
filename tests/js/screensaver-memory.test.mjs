// Unit tests for the per-device screensaver toggle persistence. Default is
// OFF; only the literal "1" reads as enabled, and a throwing/undefined
// storage must never crash the app.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  loadScreensaverEnabled,
  saveScreensaverEnabled,
  STORAGE_KEY,
} from "../../app/static/js/screensaver-memory.js";

test("load: missing value defaults to OFF", () => {
  assert.equal(loadScreensaverEnabled({ getItem: () => null }), false);
});

test("load: '1' reads as ON, anything else as OFF", () => {
  assert.equal(loadScreensaverEnabled({ getItem: () => "1" }), true);
  assert.equal(loadScreensaverEnabled({ getItem: () => "0" }), false);
  assert.equal(loadScreensaverEnabled({ getItem: () => "true" }), false);
  assert.equal(loadScreensaverEnabled({ getItem: () => "yes" }), false);
});

test("load: a throwing storage falls back to OFF (no crash)", () => {
  const fake = {
    getItem: () => {
      throw new Error("storage disabled");
    },
  };
  assert.equal(loadScreensaverEnabled(fake), false);
});

test("load: unavailable storage (undefined) falls back to OFF", () => {
  assert.equal(loadScreensaverEnabled(undefined), false);
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
  assert.equal(loadScreensaverEnabled(fake), true);
  saveScreensaverEnabled(false, fake);
  assert.equal(loadScreensaverEnabled(fake), false);
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
