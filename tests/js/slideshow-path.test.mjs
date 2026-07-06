// Unit tests for the pure path helpers of the navigable slideshow browser.
// Breadcrumb segments must never reach above the media root, even for
// malformed input.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  breadcrumbSegments,
  shortName,
} from "../../app/static/admin/slideshow-path.js";

test("shortName: last segment, or whole path when none", () => {
  assert.equal(shortName("/media/Photos/Urlaub"), "Urlaub");
  assert.equal(shortName("/media"), "media");
  assert.equal(shortName("/"), "/");
});

test("breadcrumb: root only when base equals root", () => {
  const segs = breadcrumbSegments("/media", "/media");
  assert.deepEqual(segs, [{ name: "media", path: "/media" }]);
});

test("breadcrumb: one level down", () => {
  const segs = breadcrumbSegments("/media", "/media/Photos");
  assert.deepEqual(segs, [
    { name: "media", path: "/media" },
    { name: "Photos", path: "/media/Photos" },
  ]);
});

test("breadcrumb: two levels down accumulates paths", () => {
  const segs = breadcrumbSegments("/media", "/media/Photos/Urlaub");
  assert.deepEqual(segs, [
    { name: "media", path: "/media" },
    { name: "Photos", path: "/media/Photos" },
    { name: "Urlaub", path: "/media/Photos/Urlaub" },
  ]);
});

test("breadcrumb: base not below root falls back to root only", () => {
  const segs = breadcrumbSegments("/media", "/etc/passwd");
  assert.deepEqual(segs, [{ name: "media", path: "/media" }]);
});

test("breadcrumb: tolerates a trailing slash on root", () => {
  const segs = breadcrumbSegments("/media/", "/media/Photos");
  assert.deepEqual(segs, [
    { name: "media", path: "/media" },
    { name: "Photos", path: "/media/Photos" },
  ]);
});
