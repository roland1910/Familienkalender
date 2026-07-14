// Admin page entry point: wires the modules together (source list,
// add wizards, settings, manual sync). Foreign strings are rendered
// exclusively via textContent — see dom.js.

import * as api from "./api.js";
import { initBirthdaysWizard, resetBirthdaysWizard } from "./birthdays-wizard.js";
import { initBusySync, loadBusySync } from "./busy-sync.js";
import { initChangelog, loadChangelog } from "./changelog.js";
import { byId, showMessage, withPageError } from "./dom.js";
import { initFeed, loadFeed } from "./feed.js";
import { initGoogleWizard, resetGoogleWizard } from "./google-wizard.js";
import { initNextcloudWizard, resetNextcloudWizard } from "./nextcloud-wizard.js";
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

function init() {
  // Opening one wizard closes (and fully resets) the others.
  const resetOthers = (keep) => () => {
    if (keep !== "nextcloud") resetNextcloudWizard();
    if (keep !== "google") resetGoogleWizard();
    if (keep !== "birthdays") resetBirthdaysWizard();
  };
  initNextcloudWizard({ onCreated: refreshSources, beforeOpen: resetOthers("nextcloud") });
  initGoogleWizard({ onCreated: refreshSources, beforeOpen: resetOthers("google") });
  initBirthdaysWizard({ onCreated: refreshSources, beforeOpen: resetOthers("birthdays") });
  initSettings();
  initFeed();
  initSlideshow();
  initBusySync();
  initChangelog();
  initSync();
  withPageError(async () => {
    await Promise.all([
      refreshSources(),
      loadSettings(),
      loadFeed(),
      loadSlideshow(),
      loadBusySync(),
      loadChangelog(),
    ]);
  });
}

init();
