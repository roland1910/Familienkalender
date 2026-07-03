// Normalization of API events for rendering.
//
// The API delivers timed events as ISO datetimes in local time and all-day
// events as plain dates with an exclusive end (iCalendar semantics). Parsed
// events carry startDay/endDayInclusive so every view can ask "which calendar
// days does this event touch?" without repeating the semantics.

import { addDays, fromISODate, isSameDay, startOfDay, toISODate } from "./dates.js";

export function parseEvent(raw) {
  if (raw.all_day) {
    const startDay = fromISODate(raw.start);
    return {
      ...raw,
      start: null,
      end: null,
      startDay,
      endDayInclusive: addDays(fromISODate(raw.end), -1),
    };
  }
  const start = new Date(raw.start);
  const end = new Date(raw.end);
  // An event ending exactly at midnight belongs to the previous day.
  const lastMoment = new Date(Math.max(end.getTime() - 1, start.getTime()));
  return {
    ...raw,
    start,
    end,
    startDay: startOfDay(start),
    endDayInclusive: startOfDay(lastMoment),
  };
}

export function isMultiDay(event) {
  return event.endDayInclusive.getTime() > event.startDay.getTime();
}

export function spansFullDays(event) {
  return event.all_day || isMultiDay(event);
}

function compareForDay(day) {
  return (a, b) => {
    // Full-day bars first, then by start time, then by title.
    const aFull = spansFullDays(a) ? 0 : 1;
    const bFull = spansFullDays(b) ? 0 : 1;
    if (aFull !== bFull) return aFull - bFull;
    const aTime = a.start && isSameDay(a.startDay, day) ? a.start.getTime() : 0;
    const bTime = b.start && isSameDay(b.startDay, day) ? b.start.getTime() : 0;
    if (aTime !== bTime) return aTime - bTime;
    return a.title.localeCompare(b.title, "de");
  };
}

export function groupEventsByDay(events, rangeStart, rangeEndInclusive) {
  const byDay = new Map();
  for (const event of events) {
    const firstDay = event.startDay < rangeStart ? rangeStart : event.startDay;
    const lastDay =
      event.endDayInclusive > rangeEndInclusive ? rangeEndInclusive : event.endDayInclusive;
    for (let day = firstDay; day <= lastDay; day = addDays(day, 1)) {
      const key = toISODate(day);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(event);
    }
  }
  for (const [key, dayEvents] of byDay) {
    dayEvents.sort(compareForDay(fromISODate(key)));
  }
  return byDay;
}
