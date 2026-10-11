// Unit tests for the pure parts of the admin tab shell: resolving a hash
// to a tab (including deep links into a section and malformed input) and
// the German badge wording that doubles as the screen-reader text.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_TAB,
  TABS,
  normalizeHash,
  resolveTarget,
} from "../../app/static/admin/admin-nav.js";

// Stand-in for "which panel contains the element with this id?" — in the
// browser this is a closest(".admin-panel") lookup on the real document.
const PANELS = {
  "sources-heading": "tab-quellen",
  "google-form": "tab-quellen",
  "slideshow-heading": "tab-anzeige",
  "changelog-heading": "tab-durchsicht",
  "busy-heading": "tab-syncs",
  // A section that is in the document but outside any panel.
  "page-message": null,
};
const panelOf = (id) => PANELS[id] ?? null;

test("the four tabs stand in the agreed order and Quellen is the default", () => {
  assert.deepEqual(
    TABS.map((tab) => tab.hash),
    ["quellen", "syncs", "anzeige", "durchsicht"],
  );
  assert.deepEqual(
    TABS.map((tab) => tab.label),
    ["Quellen", "Syncs & Abo", "Anzeige", "Durchsicht"],
  );
  assert.equal(DEFAULT_TAB, "quellen");
});

test("normalizeHash strips the leading # and decodes percent escapes", () => {
  assert.equal(normalizeHash("#syncs"), "syncs");
  assert.equal(normalizeHash("syncs"), "syncs");
  assert.equal(normalizeHash("#busy%2Dheading"), "busy-heading");
  assert.equal(normalizeHash(""), "");
  assert.equal(normalizeHash("#"), "");
});

test("normalizeHash never throws on malformed or non-string input", () => {
  // A lone % is not decodable — decodeURIComponent would throw.
  assert.equal(normalizeHash("#100%"), "100%");
  assert.equal(normalizeHash(undefined), "");
  assert.equal(normalizeHash(null), "");
  assert.equal(normalizeHash(42), "");
});

test("no hash at all means Quellen — the last tab is deliberately not remembered", () => {
  assert.deepEqual(resolveTarget("", panelOf), { tab: "quellen", scrollTo: null });
  assert.deepEqual(resolveTarget("#", panelOf), { tab: "quellen", scrollTo: null });
});

test("a tab hash selects that tab and scrolls nowhere", () => {
  for (const { hash } of TABS) {
    assert.deepEqual(resolveTarget(`#${hash}`, panelOf), { tab: hash, scrollTo: null });
  }
});

test("a deep link to a section opens its tab and scrolls to the section", () => {
  assert.deepEqual(resolveTarget("#slideshow-heading", panelOf), {
    tab: "anzeige",
    scrollTo: "slideshow-heading",
  });
  assert.deepEqual(resolveTarget("#changelog-heading", panelOf), {
    tab: "durchsicht",
    scrollTo: "changelog-heading",
  });
  assert.deepEqual(resolveTarget("#google-form", panelOf), {
    tab: "quellen",
    scrollTo: "google-form",
  });
});

test("an unknown hash falls back to Quellen instead of throwing", () => {
  assert.deepEqual(resolveTarget("#gibt-es-nicht", panelOf), {
    tab: "quellen",
    scrollTo: null,
  });
  // Known element, but it lives outside every panel (the global message).
  assert.deepEqual(resolveTarget("#page-message", panelOf), {
    tab: "quellen",
    scrollTo: null,
  });
  // A lookup that blows up must not take the page down with it.
  assert.deepEqual(
    resolveTarget("#boom", () => {
      throw new Error("kaputt");
    }),
    { tab: "quellen", scrollTo: null },
  );
  // A panel id that is not one of ours.
  assert.deepEqual(resolveTarget("#sources-heading", () => "tab-erfunden"), {
    tab: "quellen",
    scrollTo: null,
  });
});
