// Birthdays add-source wizard (Google Contacts / People API). Steps:
// loading (settings check) → credentials (only when no client id/secret
// stored yet) → auth (consent link with contacts scope + code paste) →
// select (name only; no calendar to pick, People API has none, and no
// display mode because birthdays are always shown "full").

import * as api from "./api.js";
import { byId, showMessage } from "./dom.js";
import { createWizard, runWizardAction } from "./wizard-shared.js";

const wizard = createWizard("birthdays-form", {
  loading: [],
  credentials: ["b-step-credentials"],
  auth: ["b-step-auth"],
  select: ["b-step-auth", "b-step-select"],
});

// Claim ticket of the current pending OAuth flow (returned by
// googleContactsConnect, consumed by createSource, discarded on reset).
let flowId = null;

export function resetBirthdaysWizard() {
  if (flowId) {
    // Abort the pending flow so its parked tokens are removed server-side.
    api.deleteGooglePending(flowId).catch(() => {});
    flowId = null;
  }
  wizard.hide();
  byId("b-auth-link").hidden = true;
  showMessage(byId("b-error"), "");
  for (const id of ["b-client-id", "b-client-secret", "b-code"]) {
    byId(id).value = "";
  }
  // The display name has a sensible default; restore it on reset.
  byId("b-name").value = "Geburtstage";
}

async function startAuthStep() {
  wizard.show("auth");
  const { auth_url: authUrl } = await api.googleContactsAuthUrl();
  const link = byId("b-auth-link");
  link.href = authUrl;
  link.hidden = false;
}

export function initBirthdaysWizard({ onCreated, beforeOpen }) {
  byId("btn-add-birthdays").addEventListener("click", () =>
    runWizardAction(byId("b-error"), async () => {
      beforeOpen();
      resetBirthdaysWizard();
      wizard.show("loading");
      const settings = await api.getSettings();
      if (settings.google_credentials.configured) {
        await startAuthStep();
      } else {
        wizard.show("credentials");
      }
    }),
  );
  byId("b-cancel").addEventListener("click", resetBirthdaysWizard);

  byId("b-save-credentials").addEventListener("click", () =>
    runWizardAction(byId("b-error"), async () => {
      await api.saveGoogleCredentials(
        byId("b-client-id").value.trim(),
        byId("b-client-secret").value,
      );
      await startAuthStep();
    }),
  );

  byId("b-connect").addEventListener("click", () =>
    runWizardAction(byId("b-error"), async () => {
      const { flow_id: newFlowId } = await api.googleContactsConnect(byId("b-code").value);
      flowId = newFlowId;
      wizard.show("select");
    }),
  );

  byId("b-save").addEventListener("click", () =>
    runWizardAction(byId("b-error"), async () => {
      const name = byId("b-name").value.trim() || "Geburtstage";
      await api.createSource({
        // Birthdays are all-day events (always family relevant), so the
        // display mode has no effect — the backend forces "full".
        type: "google_contacts",
        name,
        display_mode: "full",
        config: {},
        flow_id: flowId,
      });
      // The backend adopted the tokens — the reset must not delete them.
      flowId = null;
      resetBirthdaysWizard();
      await onCreated();
    }),
  );
}
