// Logic shared by the add-source wizards (Nextcloud and Google):
// named step states instead of scattered hidden-toggling, the calendar
// selection with name prefill, and the error display around wizard
// actions.

import { byId, el, showMessage } from "./dom.js";

// A wizard form with named steps. `show(step)` displays the form and
// exactly the sections listed for that step (a step may list several
// section ids, e.g. the calendar selection appears below the still
// visible auth section); `hide()` collapses the whole form.
export function createWizard(formId, steps) {
  const sectionIds = [...new Set(Object.values(steps).flat())];
  return {
    show(stepName) {
      const visible = steps[stepName];
      byId(formId).hidden = false;
      for (const id of sectionIds) byId(id).hidden = !visible.includes(id);
    },
    hide() {
      byId(formId).hidden = true;
      for (const id of sectionIds) byId(id).hidden = true;
    },
  };
}

// Offer the fetched calendars in the <select> and prefill the display
// name with the first entry.
export function offerCalendars(selectNode, nameInput, calendars, calendarValue) {
  selectNode.replaceChildren();
  for (const calendar of calendars) {
    const option = el("option", "", calendar.name);
    option.value = calendarValue(calendar);
    selectNode.append(option);
  }
  nameInput.value = calendars[0].name;
}

// Picking another calendar updates the display name to match.
// Bound once at wizard init (not per fetch, which would stack listeners).
export function bindNameToSelection(selectNode, nameInput) {
  selectNode.addEventListener("change", () => {
    nameInput.value = selectNode.selectedOptions[0]?.textContent ?? "";
  });
}

// Run a wizard action; failures land in the wizard's error area.
export async function runWizardAction(errorNode, action) {
  showMessage(errorNode, "");
  try {
    await action();
  } catch (error) {
    showMessage(errorNode, error.message, true);
  }
}
