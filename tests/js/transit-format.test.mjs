// Unit tests for the departure board's pure rules.
//
// These decide what the kiosk actually shows from two metres away, so the
// interesting cases are the awkward ones: a bus that is due now, one that
// is late, one that was cancelled, and a line nobody configured a colour
// for.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  badgeShape,
  departureLabel,
  formatClock,
  lineColor,
} from "../../app/static/js/transit-format.js";

const T = (h, m) => new Date(2026, 9, 25, h, m, 0).getTime();

test("formatClock prints the local 24-hour time", () => {
  assert.equal(formatClock(T(9, 5)), "09:05");
  assert.equal(formatClock(T(15, 26)), "15:26");
});

test("formatClock stays silent on unusable input instead of printing Invalid Date", () => {
  for (const bad of [null, undefined, Number.NaN, "gleich", {}]) {
    assert.equal(formatClock(bad), "");
  }
});

test("departureLabel leads with the time and appends a real delay", () => {
  assert.equal(
    departureLabel({ time: T(15, 7), delay: 0, cancelled: false }),
    "15:07",
  );
  assert.equal(
    departureLabel({ time: T(15, 8), delay: 3, cancelled: false }),
    "15:08 +3",
  );
});

test("departureLabel ignores a delay of zero or nonsense", () => {
  for (const delay of [0, null, undefined, "spät", Number.NaN]) {
    assert.equal(
      departureLabel({ time: T(15, 8), delay, cancelled: false }),
      "15:08",
    );
  }
});

test("a cancelled run says so in words, not only in colour", () => {
  // Colour alone is no message on a kiosk nobody is standing in front of.
  assert.equal(
    departureLabel({ time: T(15, 4), delay: null, cancelled: true }),
    "15:04 entfällt",
  );
});

test("the S3 badge carries the official colour from the MVV artwork", () => {
  // Taken from the Wikimedia SVG Roland linked (public domain): #7B107D.
  assert.equal(lineColor("sbahn", "S3"), "#7B107D");
  assert.equal(badgeShape("sbahn"), "capsule");
});

test("two-digit buses are MetroBus, three-digit are StadtBus", () => {
  // Munich numbers its MetroBus lines with two digits and its StadtBus
  // lines with three, which is exactly the colour split.
  assert.notEqual(lineColor("bus", "56"), lineColor("bus", "143"));
  assert.equal(lineColor("bus", "56"), lineColor("bus", "62"));
  assert.equal(lineColor("bus", "143"), lineColor("bus", "160"));
  assert.equal(badgeShape("bus"), "rounded");
});

test("an unknown line still gets a drawable badge", () => {
  // Never return undefined into a fill attribute — a missing colour must
  // not leave an invisible badge on the wall.
  for (const [kind, label] of [["tram", "19"], ["", ""], [undefined, undefined]]) {
    const color = lineColor(kind, label);
    assert.match(color, /^#[0-9A-Fa-f]{6}$/, `${kind}/${label}`);
    assert.ok(["capsule", "rounded"].includes(badgeShape(kind)));
  }
});
