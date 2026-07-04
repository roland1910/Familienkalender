// Week view: 7 columns Mon-Sun. Full-day and multi-day events appear as
// bars in a lane-stacked section on top; timed single-day events are
// positioned on a scrollable 24h grid (06:00-22:00 initially visible).

import { colorForSource } from "./colors.js";
import {
  addDays,
  formatTime,
  isSameDay,
  startOfWeek,
  toISODate,
  WEEKDAY_NAMES_SHORT,
} from "./dates.js";
import { el } from "./dom.js";
import { groupEventsByDay, spansFullDays } from "./events.js";
import { openDayPopover } from "./popover.js";

export const HOUR_HEIGHT_PX = 60;
const SCROLL_TO_HOUR = 6;
const MIN_EVENT_HEIGHT_PX = 24;
// Rendering caps: foreign calendars can deliver arbitrarily many events, so
// the DOM size per view stays bounded. Overflow goes to the day popover.
const MAX_ALLDAY_LANES = 5;
const MAX_TIMED_EVENTS_PER_DAY = 30;

export function weekRange(anchor) {
  const start = startOfWeek(anchor);
  return { start, end: addDays(start, 6) };
}

// Greedy interval scheduling, shared by the all-day bars (integer columns)
// and the timed events (millisecond timestamps): each item goes into the
// first lane that is free at its start. getStart/getEnd must use
// exclusive-end semantics (a lane is free when its last end <= the next
// start) and the items must be pre-sorted by start. Sets item.lane and
// returns the number of lanes used.
function assignLanes(items, getStart, getEnd) {
  const laneEnds = [];
  for (const item of items) {
    let lane = laneEnds.findIndex((end) => end <= getStart(item));
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(0);
    }
    laneEnds[lane] = getEnd(item);
    item.lane = lane;
  }
  return laneEnds.length;
}

// -- top section: full-day / multi-day bars ------------------------------

// All events touching the given day, sorted like the month view sorts them.
function eventsForDay(events, day) {
  return groupEventsByDay(events, day, day).get(toISODate(day)) ?? [];
}

function moreButton(hiddenCount, events, day) {
  const more = el("button", "more-button", `+${hiddenCount} weitere`);
  more.type = "button";
  more.addEventListener("click", () => openDayPopover(day, eventsForDay(events, day)));
  return more;
}

function buildAllDaySection(events, start) {
  const section = el("div", "week-allday");
  const bars = events
    .filter(spansFullDays)
    .map((event) => ({
      event,
      startCol: Math.max(0, Math.round((event.startDay - start) / 86400000)),
      endCol: Math.min(6, Math.round((event.endDayInclusive - start) / 86400000)),
    }))
    .filter((bar) => bar.startCol <= 6 && bar.endCol >= 0)
    .sort((a, b) => a.startCol - b.startCol || b.endCol - a.endCol);
  // endCol is inclusive; +1 turns it into the exclusive end assignLanes expects.
  const laneCount = assignLanes(
    bars,
    (bar) => bar.startCol,
    (bar) => bar.endCol + 1,
  );
  // With more lanes than fit, the last row becomes the "+N weitere" buttons.
  const visibleLanes = laneCount > MAX_ALLDAY_LANES ? MAX_ALLDAY_LANES - 1 : laneCount;
  for (const bar of bars) {
    if (bar.lane >= visibleLanes) continue;
    const node = el("div", "allday-bar");
    node.style.setProperty("--source-color", colorForSource(bar.event.source_id));
    // +2: column 1 is the hour gutter.
    node.style.gridColumn = `${bar.startCol + 2} / ${bar.endCol + 3}`;
    node.style.gridRow = String(bar.lane + 1);
    node.append(el("span", "chip-title", bar.event.title));
    section.append(node);
  }
  for (let col = 0; col < 7 && laneCount > visibleLanes; col += 1) {
    const hidden = bars.filter(
      (bar) => bar.lane >= visibleLanes && bar.startCol <= col && bar.endCol >= col,
    ).length;
    if (hidden === 0) continue;
    const more = moreButton(hidden, events, addDays(start, col));
    more.style.gridColumn = String(col + 2);
    more.style.gridRow = String(MAX_ALLDAY_LANES);
    section.append(more);
  }
  return section;
}

// -- time grid: timed single-day events ----------------------------------

function layoutTimedEvents(dayEvents) {
  // Overlapping events share the column width: greedy lane assignment
  // inside clusters of transitively overlapping events.
  const sorted = [...dayEvents].sort((a, b) => a.start - b.start || a.end - b.end);
  const laid = [];
  let cluster = [];
  let clusterEnd = null;

  const flush = () => {
    const laneCount = assignLanes(
      cluster,
      (item) => item.start.getTime(),
      (item) => item.end.getTime(),
    );
    for (const item of cluster) {
      item.laneCount = laneCount;
      laid.push(item);
    }
    cluster = [];
  };

  for (const event of sorted) {
    const item = { event, start: event.start, end: event.end };
    if (cluster.length > 0 && item.start.getTime() >= clusterEnd) flush();
    clusterEnd = Math.max(clusterEnd ?? 0, item.end.getTime());
    cluster.push(item);
  }
  if (cluster.length > 0) flush();
  return laid;
}

function minutesIntoDay(moment, day) {
  if (!isSameDay(moment, day)) {
    return moment < day ? 0 : 24 * 60;
  }
  return moment.getHours() * 60 + moment.getMinutes();
}

function buildDayColumn(day, dayEvents, today, allEvents) {
  const column = el("div", "week-day-column");
  column.dataset.date = toISODate(day);
  if (isSameDay(day, today)) column.classList.add("today");
  // Cap the rendered events per day; the earliest ones win, the rest is
  // reachable through the "+N weitere" popover.
  let visible = dayEvents;
  if (dayEvents.length > MAX_TIMED_EVENTS_PER_DAY) {
    visible = [...dayEvents]
      .sort((a, b) => a.start - b.start || a.end - b.end)
      .slice(0, MAX_TIMED_EVENTS_PER_DAY);
  }
  for (const item of layoutTimedEvents(visible)) {
    const startMinutes = minutesIntoDay(item.start, day);
    const endMinutes = Math.max(
      minutesIntoDay(item.end, day),
      startMinutes + (MIN_EVENT_HEIGHT_PX / HOUR_HEIGHT_PX) * 60,
    );
    const node = el("div", "timed-event");
    node.style.setProperty("--source-color", colorForSource(item.event.source_id));
    node.style.top = `${(startMinutes / 60) * HOUR_HEIGHT_PX}px`;
    node.style.height = `${((endMinutes - startMinutes) / 60) * HOUR_HEIGHT_PX}px`;
    const width = 100 / item.laneCount;
    node.style.left = `${item.lane * width}%`;
    node.style.width = `calc(${width}% - 2px)`;
    node.append(el("span", "chip-time", formatTime(item.event.start)));
    node.append(el("span", "chip-title", item.event.title));
    node.title = item.event.title;
    column.append(node);
  }
  if (visible.length < dayEvents.length) {
    const more = moreButton(dayEvents.length - visible.length, allEvents, day);
    more.classList.add("timed-more");
    column.append(more);
  }
  return column;
}

function buildHeader(start, today, events, tags) {
  const header = el("div", "week-header");
  header.append(el("span", "week-gutter-spacer"));
  for (let index = 0; index < 7; index += 1) {
    const day = addDays(start, index);
    // The column header is the touch target for the day popover (tag picker).
    const label = el("button", "week-day-label");
    label.type = "button";
    if (isSameDay(day, today)) label.classList.add("today");
    label.append(el("span", "week-day-name", WEEKDAY_NAMES_SHORT[index]));
    label.append(el("span", "week-day-date", `${day.getDate()}.${day.getMonth() + 1}.`));
    const dayTags = tags[toISODate(day)] ?? [];
    if (dayTags.length > 0) {
      // Emojis come from the server whitelist and go through textContent.
      label.append(el("span", "day-tags", dayTags.join("")));
    }
    label.addEventListener("click", () => openDayPopover(day, eventsForDay(events, day)));
    header.append(label);
  }
  return header;
}

export function renderWeekView(container, anchor, events, today, tags = {}) {
  const { start } = weekRange(anchor);
  const view = el("div", "week-view");
  view.append(buildHeader(start, today, events, tags));
  view.append(buildAllDaySection(events, start));

  const scroll = el("div", "week-scroll");
  const grid = el("div", "week-grid");
  grid.style.height = `${24 * HOUR_HEIGHT_PX}px`;

  const gutter = el("div", "week-gutter");
  for (let hour = 0; hour < 24; hour += 1) {
    const label = el("div", "hour-label", `${String(hour).padStart(2, "0")}:00`);
    label.style.top = `${hour * HOUR_HEIGHT_PX}px`;
    gutter.append(label);
  }
  grid.append(gutter);

  const lines = el("div", "hour-lines");
  for (let hour = 1; hour < 24; hour += 1) {
    const line = el("div", "hour-line");
    line.style.top = `${hour * HOUR_HEIGHT_PX}px`;
    lines.append(line);
  }
  grid.append(lines);

  for (let index = 0; index < 7; index += 1) {
    const day = addDays(start, index);
    const dayEvents = events.filter(
      (event) => !spansFullDays(event) && event.startDay <= day && event.endDayInclusive >= day,
    );
    grid.append(buildDayColumn(day, dayEvents, today, events));
  }
  scroll.append(grid);
  view.append(scroll);
  container.replaceChildren(view);
  scroll.scrollTop = SCROLL_TO_HOUR * HOUR_HEIGHT_PX;
}
