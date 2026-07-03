// German calendar date helpers. Weeks start on Monday.

export const MONTH_NAMES = [
  "Januar", "Februar", "März", "April", "Mai", "Juni",
  "Juli", "August", "September", "Oktober", "November", "Dezember",
];

export const WEEKDAY_NAMES_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

const MS_PER_WEEK = 7 * 24 * 3600 * 1000;

export function toISODate(day) {
  const year = day.getFullYear();
  const month = String(day.getMonth() + 1).padStart(2, "0");
  const dayOfMonth = String(day.getDate()).padStart(2, "0");
  return `${year}-${month}-${dayOfMonth}`;
}

export function fromISODate(iso) {
  const [year, month, day] = iso.split("-").map(Number);
  return new Date(year, month - 1, day);
}

export function startOfDay(moment) {
  return new Date(moment.getFullYear(), moment.getMonth(), moment.getDate());
}

export function addDays(day, days) {
  const result = new Date(day);
  result.setDate(result.getDate() + days);
  return result;
}

export function addMonths(day, months) {
  // Anchor on the first of the month so no day-of-month overflow can occur.
  return new Date(day.getFullYear(), day.getMonth() + months, 1);
}

export function startOfMonth(day) {
  return new Date(day.getFullYear(), day.getMonth(), 1);
}

export function startOfWeek(day) {
  const result = startOfDay(day);
  result.setDate(result.getDate() - ((result.getDay() + 6) % 7));
  return result;
}

export function isSameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

export function isoWeekNumber(day) {
  // ISO 8601: the week number is the week of that week's Thursday.
  const thursday = startOfDay(day);
  thursday.setDate(thursday.getDate() + 3 - ((thursday.getDay() + 6) % 7));
  const firstThursday = new Date(thursday.getFullYear(), 0, 4);
  firstThursday.setDate(firstThursday.getDate() + 3 - ((firstThursday.getDay() + 6) % 7));
  return 1 + Math.round((thursday - firstThursday) / MS_PER_WEEK);
}

export function formatDayMonth(day) {
  return `${day.getDate()}. ${MONTH_NAMES[day.getMonth()]}`;
}

export function formatTime(moment) {
  const hours = String(moment.getHours()).padStart(2, "0");
  const minutes = String(moment.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}
