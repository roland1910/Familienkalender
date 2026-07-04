// Pure tag-picker logic (no DOM): what the next emoji list looks like after
// adding/removing a tag, and whether the per-day cap is reached. Extracted
// out of popover.js so this logic is unit-testable with plain node --test,
// without a DOM stub or fixture.

/** Whether `current` has already reached `maxTagsPerDay` (add buttons disable). */
export function isAtTagCap(current, maxTagsPerDay) {
  return current.length >= maxTagsPerDay;
}

/** The emoji list after removing one occurrence of `emoji` from `current`. */
export function withoutTag(current, emoji) {
  return current.filter((item) => item !== emoji);
}

/** The emoji list after appending `emoji` to `current` (no dedup check here —
 * the picker only offers emojis not already present; the server dedups too). */
export function withTag(current, emoji) {
  return [...current, emoji];
}
