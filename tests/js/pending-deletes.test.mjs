// Unit tests for the pure parts of the admin "Zum Löschen vorgemerkt"
// section: the two-tap label of the destructive button and the German
// wording around a marked-at timestamp.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deleteButtonLabel,
  formatMarkedAt,
} from "../../app/static/admin/pending-deletes.js";

test("deleteButtonLabel: the first tap only arms the button", () => {
  // Same two-tap pattern as the mirror cleanup: deleting a photo from the
  // NAS for good must never be one stray tap away.
  assert.equal(deleteButtonLabel(false), "Endgültig löschen");
  assert.match(deleteButtonLabel(true), /erneut tippen/);
  assert.notEqual(deleteButtonLabel(true), deleteButtonLabel(false));
});

test("formatMarkedAt: a timestamp becomes a German sentence", () => {
  const text = formatMarkedAt("2026-10-09T18:30:00+00:00", {
    locale: "de-DE",
    timeZone: "Europe/Berlin",
  });
  assert.match(text, /^Vorgemerkt am /);
  assert.match(text, /9\.10\.2026/);
  assert.match(text, /20:30/); // 18:30 UTC in Europe/Berlin
});

test("formatMarkedAt: a missing or unreadable timestamp says nothing", () => {
  // The backend always sends one, but an unparsable value must not put
  // "Invalid Date" in front of Roland.
  for (const value of [undefined, null, "", "irgendwann", 42, {}]) {
    assert.equal(formatMarkedAt(value), "");
  }
});
