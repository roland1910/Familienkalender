// Unit tests for the pure tag-picker logic, run with the built-in node:test
// runner (npm run test:js) — no DOM stub needed since the functions are pure.

import assert from "node:assert/strict";
import { test } from "node:test";

import { isAtTagCap, withoutTag, withTag } from "../../app/static/js/tag-picker.js";

// -- isAtTagCap --------------------------------------------------------------

test("isAtTagCap: false below the cap", () => {
  assert.equal(isAtTagCap(["😀", "⭐"], 3), false);
});

test("isAtTagCap: true exactly at the cap", () => {
  assert.equal(isAtTagCap(["😀", "⭐", "🎉"], 3), true);
});

test("isAtTagCap: true above the cap", () => {
  assert.equal(isAtTagCap(["😀", "⭐", "🎉", "🎂"], 3), true);
});

test("isAtTagCap: empty list is never at cap (cap > 0)", () => {
  assert.equal(isAtTagCap([], 3), false);
});

// -- withoutTag ---------------------------------------------------------------

test("withoutTag: removes the matching emoji", () => {
  assert.deepEqual(withoutTag(["😀", "⭐", "🎉"], "⭐"), ["😀", "🎉"]);
});

test("withoutTag: removing the only tag yields an empty list", () => {
  assert.deepEqual(withoutTag(["😀"], "😀"), []);
});

test("withoutTag: removing an emoji not present leaves the list untouched", () => {
  assert.deepEqual(withoutTag(["😀", "⭐"], "🎉"), ["😀", "⭐"]);
});

test("withoutTag: does not mutate the input array", () => {
  const current = ["😀", "⭐"];
  withoutTag(current, "😀");
  assert.deepEqual(current, ["😀", "⭐"]);
});

// -- withTag -------------------------------------------------------------------

test("withTag: appends to an empty list", () => {
  assert.deepEqual(withTag([], "😀"), ["😀"]);
});

test("withTag: appends after existing tags, preserving order", () => {
  assert.deepEqual(withTag(["😀", "⭐"], "🎉"), ["😀", "⭐", "🎉"]);
});

test("withTag: does not mutate the input array", () => {
  const current = ["😀"];
  withTag(current, "⭐");
  assert.deepEqual(current, ["😀"]);
});
