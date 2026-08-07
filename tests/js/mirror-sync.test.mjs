// Node unit tests for the pure mirror-sync status/label helpers.

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatStatus, toggleButtonLabel } from "../../app/static/admin/mirror-sync.js";

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
