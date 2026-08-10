// Node unit tests for the pure mirror-sync status/label helpers.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cleanupButtonLabel,
  cleanupNoteText,
  formatStatus,
  skipWarningText,
  toggleButtonLabel,
} from "../../app/static/admin/mirror-sync.js";

test("no last_run yields the never-ran text", () => {
  assert.equal(formatStatus(null), "Noch nie gelaufen.");
  assert.equal(formatStatus({ last_run: null }), "Noch nie gelaufen.");
});

test("successful run reports the number of mirrored appointments", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_mirrors: 12, conflicts: 0, error: null },
    { locale: "en-US" },
  );
  assert.match(line, /12 gespiegelte Termine\./);
  assert.match(line, /Letzter Lauf/);
  assert.ok(!line.includes("Konflikt"));
});

test("conflicts are surfaced as a retry hint, not as an error", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_mirrors: 12, conflicts: 2, error: null },
    { locale: "en-US" },
  );
  assert.match(line, /2 Konflikt\(e\)/);
  assert.match(line, /erneut versucht/);
});

test("error run surfaces the sanitized error", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_mirrors: 0, error: "HTTP 502" },
    { locale: "en-US" },
  );
  assert.match(line, /Fehler – HTTP 502/);
});

test("toggle button label reflects the current on/off state", () => {
  assert.equal(toggleButtonLabel(false), "Spiegel-Sync ist AUS – einschalten");
  assert.equal(toggleButtonLabel(true), "Spiegel-Sync ist AN – ausschalten");
});

test("a normal run shows no skip warning", () => {
  assert.equal(skipWarningText(null), "");
  assert.equal(skipWarningText({}), "");
  assert.equal(skipWarningText({ skipped: false, skip_reason: "source_error" }), "");
});

test("each skip reason gets its own German explanation", () => {
  const failed = skipWarningText({ skipped: true, skip_reason: "source_error" });
  assert.match(failed, /nicht gel(ö|oe)scht|nichts gel(ö|oe)scht/i);
  assert.match(failed, /Quelle/);

  const empty = skipWarningText({ skipped: true, skip_reason: "empty_result" });
  assert.match(empty, /keine Termine|leer/i);
  assert.ok(empty !== failed);

  const none = skipWarningText({ skipped: true, skip_reason: "no_sources" });
  assert.match(none, /Quelle/);
  assert.ok(none !== failed && none !== empty);
});

test("an unknown skip reason still explains that nothing was changed", () => {
  const text = skipWarningText({ skipped: true, skip_reason: "voellig-neu" });
  assert.ok(text.length > 0);
  assert.match(text, /(ü|ue)bersprungen/i);
});

test("the cleanup button asks for a second tap before it acts", () => {
  const idle = cleanupButtonLabel(false);
  const armed = cleanupButtonLabel(true);
  assert.match(idle, /Kopien/);
  assert.ok(!/erneut/i.test(idle));
  assert.match(armed, /erneut/i);
  assert.notEqual(idle, armed);
});

test("no cleanup queued means no note at all", () => {
  assert.equal(cleanupNoteText(null), "");
  assert.equal(cleanupNoteText({ cleanup_pending: 0, cleanup_failed: false }), "");
});

test("a queued cleanup says when it happens", () => {
  const text = cleanupNoteText({ cleanup_pending: 1, cleanup_failed: false });
  assert.match(text, /Sync/);
  assert.match(text, /entfernt/i);
});

test("a given-up cleanup asks Roland to check the old calendar himself", () => {
  const text = cleanupNoteText({ cleanup_pending: 0, cleanup_failed: true });
  assert.match(text, /nicht entfernt/i);
  assert.match(text, /manuell|selbst|von Hand/i);
});
