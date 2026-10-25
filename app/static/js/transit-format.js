// Pure rules for the departure board: what a row reads like, and what
// colour and shape its line badge has. DOM-free so it can be tested in
// node (tests/js/transit-format.test.mjs).

// The S3 badge colour is taken from the official MVV artwork Roland
// linked (Wikimedia, public domain): a purple capsule with white text.
const SBAHN_COLORS = {
  S1: "#16BAE7",
  S2: "#76B82A",
  S3: "#7B107D",
  S4: "#E3001B",
  S6: "#00975F",
  S7: "#943126",
  S8: "#000000",
  S20: "#ED6B86",
};
const SBAHN_FALLBACK = "#408335";

// Munich splits its buses by line number: two digits are MetroBus
// (orange), three digits are StadtBus (blue). MVG publishes no brand
// guide with hex values — these match the printed network maps closely
// and are the ONE place to correct them if they look off on the display.
const METRO_BUS_COLOR = "#EF7C00";
const CITY_BUS_COLOR = "#0065AE";
const UNKNOWN_COLOR = "#4A4A4A";

/** Badge fill for a line. Always a usable colour, never undefined. */
export function lineColor(kind, label) {
  const name = String(label ?? "")
    .trim()
    .toUpperCase();
  if (kind === "sbahn") return SBAHN_COLORS[name] ?? SBAHN_FALLBACK;
  if (kind === "bus") {
    const digits = name.replace(/\D/g, "");
    if (digits.length >= 3) return CITY_BUS_COLOR;
    if (digits.length > 0) return METRO_BUS_COLOR;
  }
  return UNKNOWN_COLOR;
}

/** "capsule" for S-Bahn (the rounded MVV pill), "rounded" for buses. */
export function badgeShape(kind) {
  return kind === "sbahn" ? "capsule" : "rounded";
}

/** Local HH:MM, or "" when the timestamp is unusable. */
export function formatClock(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  const at = new Date(ms);
  if (Number.isNaN(at.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/**
 * One row of the board: the time, a delay when there is one, and the
 * word "entfällt" for a cancelled run.
 *
 * The cancellation is spelled out rather than only coloured — nobody is
 * standing in front of this screen to interpret a shade of red.
 */
export function departureLabel(departure) {
  const clock = formatClock(departure?.time);
  if (clock === "") return "";
  if (departure?.cancelled) return `${clock} entfällt`;
  const delay = departure?.delay;
  const late = typeof delay === "number" && Number.isFinite(delay) && delay > 0;
  return late ? `${clock} +${delay}` : clock;
}
