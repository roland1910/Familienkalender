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
import { spansFullDays } from "./events.js";

export const HOUR_HEIGHT_PX = 60;
const SCROLL_TO_HOUR = 6;
const MIN_EVENT_HEIGHT_PX = 24;

export function weekRange(anchor) {
  const start = startOfWeek(anchor);
  return { start, end: addDays(start, 6) };
}

// -- top section: full-day / multi-day bars ------------------------------

function assignLanes(bars) {
  // Greedy interval scheduling: first lane whose last bar ends before this
  // bar starts. Bars are pre-sorted by start column.
  const laneEnds = [];
  for (const bar of bars) {
    let lane = laneEnds.findIndex((end) => end < bar.startCol);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(0);
    }
    laneEnds[lane] = bar.endCol;
    bar.lane = lane;
  }
  return laneEnds.length;
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
  assignLanes(bars);
  for (const bar of bars) {
    const node = el("div", "allday-bar");
    node.style.setProperty("--source-color", colorForSource(bar.event.source_id));
    // +2: column 1 is the hour gutter.
    node.style.gridColumn = `${bar.startCol + 2} / ${bar.endCol + 3}`;
    node.style.gridRow = String(bar.lane + 1);
    node.append(el("span", "chip-title", bar.event.title));
    section.append(node);
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
    const laneEnds = [];
    for (const item of cluster) {
      let lane = laneEnds.findIndex((end) => end <= item.start.getTime());
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(0);
      }
      laneEnds[lane] = item.end.getTime();
      item.lane = lane;
    }
    for (const item of cluster) {
      item.laneCount = laneEnds.length;
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

function buildDayColumn(day, dayEvents, today) {
  const column = el("div", "week-day-column");
  column.dataset.date = toISODate(day);
  if (isSameDay(day, today)) column.classList.add("today");
  for (const item of layoutTimedEvents(dayEvents)) {
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
  return column;
}

function buildHeader(start, today) {
  const header = el("div", "week-header");
  header.append(el("span", "week-gutter-spacer"));
  for (let index = 0; index < 7; index += 1) {
    const day = addDays(start, index);
    const label = el("div", "week-day-label");
    if (isSameDay(day, today)) label.classList.add("today");
    label.append(el("span", "week-day-name", WEEKDAY_NAMES_SHORT[index]));
    label.append(el("span", "week-day-date", `${day.getDate()}.${day.getMonth() + 1}.`));
    header.append(label);
  }
  return header;
}

export function renderWeekView(container, anchor, events, today) {
  const { start } = weekRange(anchor);
  const view = el("div", "week-view");
  view.append(buildHeader(start, today));
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
    grid.append(buildDayColumn(day, dayEvents, today));
  }
  scroll.append(grid);
  view.append(scroll);
  container.replaceChildren(view);
  scroll.scrollTop = SCROLL_TO_HOUR * HOUR_HEIGHT_PX;
}
