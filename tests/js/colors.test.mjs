// Unit tests for the per-source color resolution (colors.js): an
// admin-configured color wins, everything else falls back to the fixed
// palette by id. The custom color is re-validated defensively — only a
// strict #rrggbb value may ever reach a CSS custom property.

import assert from "node:assert/strict";
import { test } from "node:test";

import { colorForEvent, colorForSource } from "../../app/static/js/colors.js";

const PALETTE_0 = "#2563eb"; // palette color for id 0/8/16...
const PALETTE_1 = "#d97706"; // palette color for id 1

// -- colorForSource ----------------------------------------------------------

test("colorForSource: uses the configured color when set", () => {
  assert.equal(colorForSource({ id: 1, color: "#ff0066" }), "#ff0066");
});

test("colorForSource: empty color falls back to the palette by id", () => {
  assert.equal(colorForSource({ id: 1, color: "" }), PALETTE_1);
});

test("colorForSource: missing color property falls back to the palette", () => {
  assert.equal(colorForSource({ id: 1 }), PALETTE_1);
});

test("colorForSource: palette wraps around by id", () => {
  assert.equal(colorForSource({ id: 9, color: "" }), PALETTE_1);
});

test("colorForSource: unexpected ids map to the first palette color", () => {
  assert.equal(colorForSource({ id: undefined, color: "" }), PALETTE_0);
  assert.equal(colorForSource({ id: "kaputt", color: "" }), PALETTE_0);
});

// Defensive client-side validation: the server already enforces #rrggbb,
// but nothing that is not exactly that shape may reach a CSS variable.
for (const bad of [
  "red",
  "#FF0066", // uppercase is normalized server-side; reject it here
  "#fff",
  "#ff00667f",
  "url(x)",
  "#ff0066; background:url(x)",
  "var(--evil)",
  42,
  null,
]) {
  test(`colorForSource: invalid color ${JSON.stringify(bad)} falls back to the palette`, () => {
    assert.equal(colorForSource({ id: 1, color: bad }), PALETTE_1);
  });
}

// -- colorForEvent ------------------------------------------------------------

test("colorForEvent: uses the event's source_color when set", () => {
  assert.equal(colorForEvent({ source_id: 3, source_color: "#00aa11" }), "#00aa11");
});

test("colorForEvent: falls back to the palette by source_id", () => {
  assert.equal(colorForEvent({ source_id: 1, source_color: "" }), PALETTE_1);
});

test("colorForEvent: event and legend resolution agree for the same source", () => {
  const event = { source_id: 5, source_color: "#123abc" };
  const source = { id: 5, color: "#123abc" };
  assert.equal(colorForEvent(event), colorForSource(source));
});
