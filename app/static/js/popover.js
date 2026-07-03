// Day popover: full event list for one day (opened from "+N weitere").

import { colorForSource } from "./colors.js";
import { formatDayMonth, formatTime, isSameDay, WEEKDAY_NAMES_SHORT } from "./dates.js";
import { el } from "./dom.js";
import { spansFullDays } from "./events.js";

function backdrop() {
  return document.getElementById("day-popover");
}

export function closeDayPopover() {
  const node = backdrop();
  node.hidden = true;
  node.replaceChildren();
}

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

  const list = el("ul", "popover-list");
  for (const event of events) {
    const item = el("li", "popover-item");
    item.style.setProperty("--source-color", colorForSource(event.source_id));
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
  panel.append(list);
  node.append(panel);
  node.hidden = false;
}

export function initPopover() {
  backdrop().addEventListener("click", (clickEvent) => {
    if (clickEvent.target === backdrop()) closeDayPopover();
  });
  document.addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "Escape") closeDayPopover();
  });
}
