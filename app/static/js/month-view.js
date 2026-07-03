// Classic month grid: 6 weeks x 7 days, weeks starting Monday.

import { colorForSource } from "./colors.js";
import {
  addDays,
  formatTime,
  isSameDay,
  startOfMonth,
  startOfWeek,
  toISODate,
  WEEKDAY_NAMES_SHORT,
} from "./dates.js";
import { el } from "./dom.js";
import { groupEventsByDay, spansFullDays } from "./events.js";
import { openDayPopover } from "./popover.js";

const GRID_DAYS = 42;
// With more events than this, the last slot becomes the "+N weitere" button.
const MAX_CHIPS_PER_CELL = 4;

export function monthGridRange(anchor) {
  const gridStart = startOfWeek(startOfMonth(anchor));
  return { start: gridStart, end: addDays(gridStart, GRID_DAYS - 1) };
}

function buildChip(event, day) {
  const chip = el("div", "chip");
  chip.style.setProperty("--source-color", colorForSource(event.source_id));
  if (spansFullDays(event)) chip.classList.add("chip-allday");
  if (!spansFullDays(event) && isSameDay(event.startDay, day)) {
    chip.append(el("span", "chip-time", formatTime(event.start)));
  }
  chip.append(el("span", "chip-title", event.title));
  chip.title = event.title; // tooltip; attribute value, safe as plain text
  return chip;
}

function buildCell(day, dayEvents, anchor, today) {
  const cell = el("div", "day-cell");
  cell.dataset.date = toISODate(day);
  if (day.getMonth() !== anchor.getMonth()) cell.classList.add("other-month");
  if (isSameDay(day, today)) cell.classList.add("today");
  cell.append(el("div", "day-number", String(day.getDate())));

  const chips = el("div", "day-chips");
  let visible = dayEvents;
  let hiddenCount = 0;
  if (dayEvents.length > MAX_CHIPS_PER_CELL) {
    // -1: the "+N weitere" button takes the last of the MAX_CHIPS_PER_CELL
    // slots, so the cell never grows beyond MAX_CHIPS_PER_CELL rows.
    visible = dayEvents.slice(0, MAX_CHIPS_PER_CELL - 1);
    hiddenCount = dayEvents.length - visible.length;
  }
  for (const event of visible) {
    chips.append(buildChip(event, day));
  }
  if (hiddenCount > 0) {
    const more = el("button", "more-button", `+${hiddenCount} weitere`);
    more.type = "button";
    more.addEventListener("click", () => openDayPopover(day, dayEvents));
    chips.append(more);
  }
  cell.append(chips);
  return cell;
}

export function renderMonthView(container, anchor, events, today) {
  const { start, end } = monthGridRange(anchor);
  const byDay = groupEventsByDay(events, start, end);

  const view = el("div", "month-view");
  const header = el("div", "weekday-header");
  for (const name of WEEKDAY_NAMES_SHORT) {
    header.append(el("span", "weekday", name));
  }
  view.append(header);

  const grid = el("div", "month-grid");
  for (let index = 0; index < GRID_DAYS; index += 1) {
    const day = addDays(start, index);
    grid.append(buildCell(day, byDay.get(toISODate(day)) ?? [], anchor, today));
  }
  view.append(grid);
  container.replaceChildren(view);
}
