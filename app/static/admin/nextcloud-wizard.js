// Nextcloud (CalDAV) add-source wizard. Steps: credentials (URL,
// username, app password + connection test) → select (calendar choice,
// name, display mode).

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";
import {
  bindNameToSelection,
  createWizard,
  offerCalendars,
  runWizardAction,
} from "./wizard-shared.js";

// The credentials inputs live directly in the form, so the first step
// shows no extra section; the calendar selection appears below them.
const wizard = createWizard("nextcloud-form", {
  credentials: [],
  select: ["nc-step-select"],
});

export function resetNextcloudWizard() {
  wizard.hide();
  showMessage(byId("nc-error"), "");
  for (const id of ["nc-url", "nc-username", "nc-password", "nc-name"]) {
    byId(id).value = "";
  }
  byId("nc-calendar").replaceChildren();
}

export function initNextcloudWizard({ onCreated, beforeOpen }) {
  bindNameToSelection(byId("nc-calendar"), byId("nc-name"));

  byId("btn-add-nextcloud").addEventListener("click", () => {
    beforeOpen();
    resetNextcloudWizard();
    wizard.show("credentials");
    byId("nc-url").focus();
  });
  byId("nc-cancel").addEventListener("click", resetNextcloudWizard);

  byId("nc-test").addEventListener("click", () =>
    runWizardAction(byId("nc-error"), async () => {
      const { calendars } = await api.probeCaldav(
        byId("nc-url").value.trim(),
        byId("nc-username").value.trim(),
        byId("nc-password").value,
      );
      if (calendars.length === 0) {
        showMessage(byId("nc-error"), "Keine Kalender gefunden.", true);
        return;
      }
      offerCalendars(byId("nc-calendar"), byId("nc-name"), calendars, (calendar) => calendar.url);
      wizard.show("select");
    }),
  );

  byId("nc-save").addEventListener("click", () =>
    runWizardAction(byId("nc-error"), async () => {
      await api.createSource({
        type: "caldav",
        name: byId("nc-name").value.trim(),
        display_mode: byId("nc-mode").value,
        config: {
          url: byId("nc-url").value.trim(),
          username: byId("nc-username").value.trim(),
          app_password: byId("nc-password").value,
          calendar_url: byId("nc-calendar").value,
        },
      });
      resetNextcloudWizard();
      await onCreated();
    }),
  );
}
