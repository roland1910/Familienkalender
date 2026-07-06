// Unit tests for the per-device theme persistence (localStorage). Three
// themes (auto/light/dark); any unknown/garbage value falls back to "auto",
// and a throwing/undefined storage must never crash the app.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  loadTheme,
  nextTheme,
  normalizeTheme,
  saveTheme,
  STORAGE_KEY,
  THEMES,
} from "../../app/static/js/theme-memory.js";

test("normalizeTheme: known themes pass through", () => {
  for (const theme of THEMES) {
    assert.equal(normalizeTheme(theme), theme);
  }
});

test("normalizeTheme: unknown/garbage values fall back to auto", () => {
  for (const value of [null, undefined, "", "Dark", "disco", "0", 7, {}, []]) {
    assert.equal(normalizeTheme(value), "auto");
  }
});

test("nextTheme: cycles auto -> light -> dark -> auto", () => {
  assert.equal(nextTheme("auto"), "light");
  assert.equal(nextTheme("light"), "dark");
  assert.equal(nextTheme("dark"), "auto");
});

test("nextTheme: an invalid current theme starts from auto", () => {
  assert.equal(nextTheme("garbage"), "light");
});

test("load: missing value defaults to auto", () => {
  assert.equal(loadTheme({ getItem: () => null }), "auto");
});

test("load: known values read back, unknown falls back to auto", () => {
  assert.equal(loadTheme({ getItem: () => "dark" }), "dark");
  assert.equal(loadTheme({ getItem: () => "light" }), "light");
  assert.equal(loadTheme({ getItem: () => "auto" }), "auto");
  assert.equal(loadTheme({ getItem: () => "hacked" }), "auto");
});

test("load: a throwing storage falls back to auto (no crash)", () => {
  const fake = {
    getItem: () => {
      throw new Error("storage disabled");
    },
  };
  assert.equal(loadTheme(fake), "auto");
});

test("load: unavailable storage (undefined) falls back to auto", () => {
  assert.equal(loadTheme(undefined), "auto");
});

test("save: writes the normalized theme", () => {
  const written = {};
  const fake = {
    setItem: (key, value) => {
      written[key] = value;
    },
  };
  saveTheme("dark", fake);
  assert.equal(written[STORAGE_KEY], "dark");
  saveTheme("garbage", fake);
  assert.equal(written[STORAGE_KEY], "auto");
});

test("save: round-trips through the same fake storage", () => {
  const store = new Map();
  const fake = {
    getItem: (key) => store.get(key) ?? null,
    setItem: (key, value) => store.set(key, value),
  };
  saveTheme("dark", fake);
  assert.equal(loadTheme(fake), "dark");
  saveTheme("light", fake);
  assert.equal(loadTheme(fake), "light");
});

test("save: a throwing storage is ignored (best effort, no crash)", () => {
  const fake = {
    setItem: () => {
      throw new Error("quota exceeded");
    },
  };
  saveTheme("dark", fake);
});

test("save: unavailable storage (undefined) is ignored", () => {
  saveTheme("dark", undefined);
});
