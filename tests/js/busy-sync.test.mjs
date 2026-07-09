// Node unit tests for the pure busy-sync status formatter.

import assert from "node:assert/strict";
import { test } from "node:test";

import { formatStatus } from "../../app/static/admin/busy-sync.js";

test("no last_run yields the never-ran text", () => {
  assert.equal(formatStatus(null), "Noch nie gelaufen.");
  assert.equal(formatStatus({ last_run: null }), "Noch nie gelaufen.");
});

test("successful run reports the active block count", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_blocks: 4, error: null },
    { locale: "en-US" },
  );
  assert.match(line, /4 aktive Belegt-Blöcke\./);
  assert.match(line, /Letzter Lauf/);
});

test("error run surfaces the sanitized error", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_blocks: 0, error: "HTTP 502" },
    { locale: "en-US" },
  );
  assert.match(line, /Fehler – HTTP 502/);
});
