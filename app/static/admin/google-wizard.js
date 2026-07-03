// Google add-source wizard. Steps: loading (settings check) →
// credentials (only when no client id/secret stored yet) → auth
// (consent link + code paste) → select (calendar choice, still showing
// the auth section above it).

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";
import {
  bindNameToSelection,
  createWizard,
  offerCalendars,
  runWizardAction,
} from "./wizard-shared.js";

const wizard = createWizard("google-form", {
  loading: [],
  credentials: ["g-step-credentials"],
  auth: ["g-step-auth"],
  select: ["g-step-auth", "g-step-select"],
});

// Claim ticket of the current pending OAuth flow (returned by
// googleConnect, consumed by createSource, discarded on reset).
let flowId = null;

export function resetGoogleWizard() {
  if (flowId) {
    // Abort the pending flow so its parked tokens are removed server-side.
    api.deleteGooglePending(flowId).catch(() => {});
    flowId = null;
  }
  wizard.hide();
  byId("g-auth-link").hidden = true;
  showMessage(byId("g-error"), "");
  for (const id of ["g-client-id", "g-client-secret", "g-code", "g-name"]) {
    byId(id).value = "";
  }
  byId("g-calendar").replaceChildren();
}

async function startAuthStep() {
  wizard.show("auth");
  const { auth_url: authUrl } = await api.googleAuthUrl();
  const link = byId("g-auth-link");
  link.href = authUrl;
  link.hidden = false;
}

export function initGoogleWizard({ onCreated, beforeOpen }) {
  bindNameToSelection(byId("g-calendar"), byId("g-name"));

  byId("btn-add-google").addEventListener("click", () =>
    runWizardAction(byId("g-error"), async () => {
      beforeOpen();
      resetGoogleWizard();
      wizard.show("loading");
      const settings = await api.getSettings();
      if (settings.google_credentials.configured) {
        await startAuthStep();
      } else {
        wizard.show("credentials");
      }
    }),
  );
  byId("g-cancel").addEventListener("click", resetGoogleWizard);

  byId("g-save-credentials").addEventListener("click", () =>
    runWizardAction(byId("g-error"), async () => {
      await api.saveGoogleCredentials(
        byId("g-client-id").value.trim(),
        byId("g-client-secret").value,
      );
      await startAuthStep();
    }),
  );

  byId("g-connect").addEventListener("click", () =>
    runWizardAction(byId("g-error"), async () => {
      const { flow_id: newFlowId, calendars } = await api.googleConnect(byId("g-code").value);
      flowId = newFlowId;
      if (calendars.length === 0) {
        showMessage(byId("g-error"), "Keine Kalender gefunden.", true);
        return;
      }
      offerCalendars(byId("g-calendar"), byId("g-name"), calendars, (calendar) => calendar.id);
      wizard.show("select");
    }),
  );

  byId("g-save").addEventListener("click", () =>
    runWizardAction(byId("g-error"), async () => {
      await api.createSource({
        type: "google",
        name: byId("g-name").value.trim(),
        display_mode: byId("g-mode").value,
        config: { calendar_id: byId("g-calendar").value },
        flow_id: flowId,
      });
      // The backend adopted the tokens — the reset must not delete them.
      flowId = null;
      resetGoogleWizard();
      await onCreated();
    }),
  );
}
