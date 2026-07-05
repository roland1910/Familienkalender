// Day popover: full event list for one day plus the tag picker. Opened
// from "+N weitere", from the day number (month) or the column header (week).

import { putDayTags } from "./api.js";
import { colorForEvent } from "./colors.js";
import { formatDayMonth, formatTime, isSameDay, toISODate, WEEKDAY_NAMES_SHORT } from "./dates.js";
import { el } from "./dom.js";
import { spansFullDays } from "./events.js";
import { state } from "./state.js";
import { isAtTagCap, withoutTag, withTag } from "./tag-picker.js";

// Cap against event floods from foreign calendars; everything beyond this
// collapses into one "… und N weitere" line.
const MAX_POPOVER_ITEMS = 100;

// Set via initPopover: re-renders the calendar after a tag change.
let onTagsChanged = () => {};

function backdrop() {
  return document.getElementById("day-popover");
}

export function closeDayPopover() {
  const node = backdrop();
  node.hidden = true;
  node.replaceChildren();
}

// -- tag picker ------------------------------------------------------------

// Double-tap guard: while a PUT is in flight, every tag button in the
// section is disabled so a second tap cannot fire a second request for the
// same day (touch screens are prone to accidental double taps). The section
// is re-rendered (success) or re-enabled (failure) once the request settles.
function lockTagButtons(section) {
  const buttons = section.querySelectorAll(".tag-button");
  for (const button of buttons) button.disabled = true;
  return () => {
    for (const button of buttons) button.disabled = false;
  };
}

async function saveTags(day, emojis, section) {
  const iso = toISODate(day);
  const unlock = lockTagButtons(section);
  let stored;
  try {
    stored = await putDayTags(iso, emojis);
  } catch {
    unlock();
    section.querySelector(".tag-error")?.remove();
    section.append(el("p", "tag-error", "Symbol speichern fehlgeschlagen."));
    return;
  }
  if (stored.length > 0) {
    state.tags[iso] = stored;
  } else {
    delete state.tags[iso];
  }
  section.replaceWith(buildTagSection(day));
  onTagsChanged();
}

function tagButton(emoji, className, label, onTap) {
  const button = el("button", `tag-button ${className}`, emoji);
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", onTap);
  return button;
}

function buildTagSection(day) {
  const section = el("section", "popover-tags");
  const current = state.tags[toISODate(day)] ?? [];
  section.append(el("h3", "popover-tags-title", "Symbole für diesen Tag"));

  const row = el("div", "tag-row");
  for (const emoji of current) {
    const remaining = withoutTag(current, emoji);
    row.append(
      tagButton(emoji, "tag-current", `Symbol ${emoji} entfernen`, () =>
        saveTags(day, remaining, section),
      ),
    );
  }
  const atCap = isAtTagCap(current, state.maxTagsPerDay);
  for (const option of state.tagOptions) {
    if (current.includes(option.emoji)) continue;
    const button = tagButton(option.emoji, "tag-option", `Symbol ${option.emoji} hinzufügen`, () =>
      saveTags(day, withTag(current, option.emoji), section),
    );
    button.disabled = atCap;
    row.append(button);
  }
  section.append(row);

  if (state.tagOptions.length === 0) {
    section.append(el("p", "tag-hint", "Symbolauswahl konnte nicht geladen werden."));
  } else if (atCap) {
    section.append(el("p", "tag-hint", `Höchstens ${state.maxTagsPerDay} Symbole pro Tag.`));
  }
  return section;
}

// -- popover ----------------------------------------------------------------

export function openDayPopover(day, events) {
  const node = backdrop();
  node.replaceChildren();

  const panel = el("div", "popover-panel");
  const header = el("div", "popover-header");
  const weekday = WEEKDAY_NAMES_SHORT[(day.getDay() + 6) % 7];
  header.append(el("h2", "popover-title", `${weekday}, ${formatDayMonth(day)}`));
  const close = el("button", "popover-close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Schließen");
  close.addEventListener("click", closeDayPopover);
  header.append(close);
  panel.append(header);

  panel.append(buildTagSection(day));

  const list = el("ul", "popover-list");
  const shown = events.slice(0, MAX_POPOVER_ITEMS);
  for (const event of shown) {
    const item = el("li", "popover-item");
    item.style.setProperty("--source-color", colorForEvent(event));
    const time =
      spansFullDays(event) || !isSameDay(event.startDay, day)
        ? "ganztägig"
        : `${formatTime(event.start)}–${formatTime(event.end)}`;
    item.append(el("span", "popover-time", time));
    const text = el("span", "popover-text");
    text.append(el("span", "popover-event-title", event.title));
    if (event.location) {
      text.append(el("span", "popover-location", event.location));
    }
    item.append(text);
    list.append(item);
  }
  if (events.length === 0) {
    list.append(el("li", "popover-more", "Keine Termine an diesem Tag."));
  }
  if (events.length > shown.length) {
    // el() renders via textContent — no HTML sink even for hostile counts.
    list.append(el("li", "popover-more", `… und ${events.length - shown.length} weitere`));
  }
  panel.append(list);
  node.append(panel);
  node.hidden = false;
}

export function initPopover({ onTagsChanged: tagsChangedCallback } = {}) {
  if (tagsChangedCallback) onTagsChanged = tagsChangedCallback;
  backdrop().addEventListener("click", (clickEvent) => {
    if (clickEvent.target === backdrop()) closeDayPopover();
  });
  document.addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "Escape") closeDayPopover();
  });
}
