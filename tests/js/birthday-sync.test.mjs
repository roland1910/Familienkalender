// Node unit tests for the pure birthday-sync status/label helpers.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  formatStatus,
  googleHintText,
  skipWarningText,
  toggleButtonLabel,
} from "../../app/static/admin/birthday-sync.js";

test("no last_run yields the never-ran text", () => {
  assert.equal(formatStatus(null), "Noch nie gelaufen.");
  assert.equal(formatStatus({ last_run: null }), "Noch nie gelaufen.");
});

test("successful run reports both targets separately", () => {
  const line = formatStatus(
    {
      last_run: "2026-07-09T10:00:00+00:00",
      active_google: 42,
      active_caldav: 41,
      conflicts: 0,
      error: null,
    },
    { locale: "en-US" },
  );
  assert.match(line, /42 Serien in Xalt/);
  assert.match(line, /41 in Nextcloud/);
  assert.ok(!line.includes("Konflikt"));
});

test("conflicts are surfaced as a retry hint, not as an error", () => {
  const line = formatStatus(
    {
      last_run: "2026-07-09T10:00:00+00:00",
      active_google: 42,
      active_caldav: 42,
      conflicts: 3,
      error: null,
    },
    { locale: "en-US" },
  );
  assert.match(line, /3 Konflikt\(e\)/);
  assert.match(line, /erneut versucht/);
});

test("error run surfaces the sanitized error", () => {
  const line = formatStatus(
    { last_run: "2026-07-09T10:00:00+00:00", active_google: 0, error: "HTTP 502" },
    { locale: "en-US" },
  );
  assert.match(line, /Fehler – HTTP 502/);
});

test("toggle button label reflects the current on/off state", () => {
  assert.equal(toggleButtonLabel(false), "Geburtstags-Sync ist AUS – einschalten");
  assert.equal(toggleButtonLabel(true), "Geburtstags-Sync ist AN – ausschalten");
});

test("a normal run shows no skip warning", () => {
  assert.equal(skipWarningText(null), "");
  assert.equal(skipWarningText({}), "");
  assert.equal(skipWarningText({ skipped: false, skip_reason: "source_error" }), "");
});

test("each skip reason gets its own German explanation", () => {
  const failed = skipWarningText({ skipped: true, skip_reason: "source_error" });
  assert.match(failed, /nichts gel(ö|oe)scht/i);

  const empty = skipWarningText({ skipped: true, skip_reason: "empty_result" });
  assert.match(empty, /keine Geburtstage/i);
  assert.ok(empty !== failed);

  const none = skipWarningText({ skipped: true, skip_reason: "no_sources" });
  assert.match(none, /ausschalten/);
  assert.ok(none !== failed && none !== empty);
});

test("an unknown skip reason still explains that nothing was changed", () => {
  const text = skipWarningText({ skipped: true, skip_reason: "voellig-neu" });
  assert.match(text, /(ü|ue)bersprungen/i);
});

test("the Xalt target warns only when the write connection is missing", () => {
  assert.equal(googleHintText(false, false), "");
  assert.equal(googleHintText(true, true), "");
  assert.match(googleHintText(true, false), /Schreib-Verbindung/);
});
