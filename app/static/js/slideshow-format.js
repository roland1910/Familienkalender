// Pure formatting for the slideshow overlays (no DOM): the taken-at date
// shown top right and the folder trail shown top left. Extracted out of
// slideshow-view.js so both are unit-testable with plain node --test.
// Both return "" when nothing is displayable — the caller then hides the
// overlay entirely (Roland: "Wenn du nichts auslesen kannst soll einfach
// nichts da stehen.").

function pad2(number) {
  return String(number).padStart(2, "0");
}

/**
 * The (possibly partial) taken-at moment from /api/slideshow/next as a
 * German display string:
 *   full            -> "16.08.2019 17:30"
 *   date only       -> "16.08.2019"
 *   year only       -> "2019"
 *   null/unusable   -> ""
 * A partial date (year+month without day) falls back to the year; a time
 * is only shown when the full date and both hour and minute are known.
 */
export function formatTakenAt(taken) {
  if (!taken || !Number.isInteger(taken.year)) return "";
  const { year, month, day, hour, minute } = taken;
  if (!Number.isInteger(month) || !Number.isInteger(day)) return String(year);
  const datePart = `${pad2(day)}.${pad2(month)}.${year}`;
  if (!Number.isInteger(hour) || !Number.isInteger(minute)) return datePart;
  return `${datePart} ${pad2(hour)}:${pad2(minute)}`;
}

/**
 * The photo's folder segments (below the media root) joined for display:
 * ["Photos", "2019", "Urlaub"] -> "Photos › 2019 › Urlaub". Empty or
 * missing input yields "". The segments are foreign strings from the
 * network share — the caller must render the result via textContent only.
 */
export function formatFolderTrail(folders) {
  if (!Array.isArray(folders)) return "";
  return folders.filter((segment) => typeof segment === "string" && segment !== "").join(" › ");
}
