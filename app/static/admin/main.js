// Admin page entry point: wires the modules together (source list,
// add wizards, settings, manual sync). Foreign strings are rendered
// exclusively via textContent — see dom.js.

import { initAdminNav } from "./admin-nav.js";
import * as api from "./api.js";
import { initBirthdaySync, loadBirthdaySync } from "./birthday-sync.js";
import { initBirthdaysWizard, resetBirthdaysWizard } from "./birthdays-wizard.js";
import { initBusySync, loadBusySync } from "./busy-sync.js";
import { initChangelog, loadChangelog } from "./changelog.js";
import { byId, showMessage, withPageError } from "./dom.js";
import { initFeed, loadFeed } from "./feed.js";
import { initGoogleWizard, resetGoogleWizard } from "./google-wizard.js";
import { initMirrorSync, loadMirrorSync } from "./mirror-sync.js";
import { initNextcloudWizard, resetNextcloudWizard } from "./nextcloud-wizard.js";
import { initPendingDeletes, loadPendingDeletes } from "./pending-deletes.js";
import { initSettings, loadSettings } from "./settings.js";
import { initSlideshow, loadSlideshow } from "./slideshow.js";
import { refreshSources } from "./sources.js";

function initSync() {
  const button = byId("btn-sync");
  button.addEventListener("click", async () => {
    const messageNode = byId("page-message");
    button.disabled = true;
    showMessage(messageNode, "Synchronisierung läuft…");
    try {
      const { results } = await api.triggerSync();
      const errors = Object.values(results).filter((error) => error !== null);
      const total = Object.keys(results).length;
      if (errors.length === 0) {
        showMessage(messageNode, `Synchronisierung abgeschlossen (${total} Quellen).`);
      } else {
        showMessage(
          messageNode,
          `Synchronisierung abgeschlossen: ${errors.length} von ${total} Quellen mit Fehlern.`,
          true,
        );
      }
      await refreshSources();
    } catch (error) {
      showMessage(messageNode, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

// Etappe 51, Schritt 5: the three "add a ..." buttons hide behind one
// entry. Deliberately a few lines here rather than a module of its own —
// it is a show/hide with no state worth testing in isolation, and the
// three wizard IDs stay exactly what they were.
function initAddSourceEntry() {
  const entry = byId("btn-add-source");
  const kinds = byId("add-source-kinds");
  entry.addEventListener("click", () => {
    kinds.hidden = false;
    entry.hidden = true;
    // Move the focus on, so keyboard users land on the choice they asked
    // for instead of on a button that just disappeared.
    byId("btn-add-nextcloud").focus();
  });
}

function init() {
  // First: the tab shell. Until it runs, every section is visible — the
  // page degrades to the long scroll it used to be rather than to nothing.
  initAdminNav();
  // Opening one wizard closes (and fully resets) the others.
  const resetOthers = (keep) => () => {
    if (keep !== "nextcloud") resetNextcloudWizard();
    if (keep !== "google") resetGoogleWizard();
    if (keep !== "birthdays") resetBirthdaysWizard();
  };
  initAddSourceEntry();
  initNextcloudWizard({ onCreated: refreshSources, beforeOpen: resetOthers("nextcloud") });
  initGoogleWizard({ onCreated: refreshSources, beforeOpen: resetOthers("google") });
  initBirthdaysWizard({ onCreated: refreshSources, beforeOpen: resetOthers("birthdays") });
  initSettings();
  initFeed();
  initSlideshow();
  initPendingDeletes();
  initBusySync();
  initMirrorSync();
  initBirthdaySync();
  initChangelog();
  initSync();
  // EVERY loader runs at page start, also for the tabs nobody has opened yet.
  // Loading per tab on demand would save a little on the phone, but it would
  // hide exactly the failures the tab badges exist to show (a source whose
  // sync failed, photos waiting for a decision) — and that data has to be
  // fetched for the badges anyway. A failure lands in #page-message, which
  // sits ABOVE the tab bar and is therefore visible whatever tab is open.
  withPageError(async () => {
    await Promise.all([
      refreshSources(),
      loadSettings(),
      loadFeed(),
      loadSlideshow(),
      loadPendingDeletes(),
      loadBusySync(),
      loadMirrorSync(),
      loadBirthdaySync(),
      loadChangelog(),
    ]);
  });
}

init();
