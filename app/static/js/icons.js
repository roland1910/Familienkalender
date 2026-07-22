// Inline SVG icons for the header controls.
//
// WHY NOT EMOJI (Etappe 38, found on the real kiosk display): the kiosk runs
// WebKitGTK/luakit on HA-OS, which has NO font covering emoji outside the
// Basic Multilingual Plane. 📅 (U+1F4C5), 🌧️ (U+1F327) and 🖼️ (U+1F5BC)
// rendered as empty boxes (tofu) — only ⚡ (U+26A1, BMP) showed up. Since
// Etappe 37 the buttons carry no words either, so they were unusable.
// Rule from here on: NEVER use emoji for a control icon; draw it as SVG.
// (The day tags in app/models.py stay emoji on purpose — they are content
// Roland picked, not controls.)
//
// The shapes are hand-drawn on a 24x24 grid: few, thick strokes so they stay
// readable from two metres. Colours always come from `currentColor`, so an
// icon follows the theme variables and the accent fill of an active button
// without any extra rule.
//
// Built with createElementNS only — never an HTML string (see
// tests/test_frontend_static.py).

const SVG_NS = "http://www.w3.org/2000/svg";

// Shared presentation attributes. Outlines are stroked, solid bodies filled;
// both resolve to the button's text colour.
const STROKE = {
  fill: "none",
  stroke: "currentColor",
  "stroke-width": 2,
  "stroke-linecap": "round",
  "stroke-linejoin": "round",
};
const FILL = { fill: "currentColor", stroke: "none" };

function stroked(tag, attrs) {
  return { tag, attrs: { ...STROKE, ...attrs } };
}

function filled(tag, attrs) {
  return { tag, attrs: { ...FILL, ...attrs } };
}

// A sun ray from radius 7 to radius 9.5 in the given direction (degrees).
function sunRay(degrees) {
  const radians = (degrees * Math.PI) / 180;
  const dx = Math.cos(radians);
  const dy = Math.sin(radians);
  return stroked("line", {
    x1: (12 + dx * 7).toFixed(2),
    y1: (12 + dy * 7).toFixed(2),
    x2: (12 + dx * 9.5).toFixed(2),
    y2: (12 + dy * 9.5).toFixed(2),
  });
}

// Eight square teeth around the gear's ring, each rotated into place about
// the icon's centre.
function cogTeeth() {
  return [0, 45, 90, 135, 180, 225, 270, 315].map((degrees) =>
    filled("rect", {
      x: 10.4,
      y: 1.8,
      width: 3.2,
      height: 3.6,
      rx: 0.8,
      transform: `rotate(${degrees} 12 12)`,
    }),
  );
}

const ICONS = {
  // Calendar: sheet with two hanging tabs, a header band and two day cells.
  calendar: [
    stroked("rect", { x: 3, y: 5, width: 18, height: 16, rx: 2 }),
    stroked("line", { x1: 3, y1: 10, x2: 21, y2: 10 }),
    stroked("line", { x1: 8, y1: 2.5, x2: 8, y2: 6.5 }),
    stroked("line", { x1: 16, y1: 2.5, x2: 16, y2: 6.5 }),
    filled("rect", { x: 6.5, y: 13, width: 3.5, height: 3.5, rx: 0.8 }),
    filled("rect", { x: 12.5, y: 13, width: 3.5, height: 3.5, rx: 0.8 }),
  ],
  // Power: a solid lightning bolt.
  bolt: [filled("polygon", { points: "13.5 2 5 13.5 11 13.5 10.5 22 19 10.5 13 10.5 13.5 2" })],
  // Weather: a solid cloud (two discs over a rounded bar) with rain streaks.
  cloud: [
    filled("circle", { cx: 9, cy: 10.5, r: 4.2 }),
    filled("circle", { cx: 15, cy: 11, r: 3.6 }),
    filled("rect", { x: 5.2, y: 10.5, width: 13.6, height: 4.6, rx: 2.3 }),
    stroked("line", { x1: 8, y1: 17.5, x2: 6.8, y2: 21 }),
    stroked("line", { x1: 12, y1: 17.5, x2: 10.8, y2: 21 }),
    stroked("line", { x1: 16, y1: 17.5, x2: 14.8, y2: 21 }),
  ],
  // Theme "auto": a circle with one half filled (light/dark side by side).
  "theme-auto": [
    stroked("circle", { cx: 12, cy: 12, r: 8.5 }),
    filled("path", { d: "M12 3.5A8.5 8.5 0 0 1 12 20.5Z" }),
  ],
  // Theme "light": sun disc with eight rays.
  "theme-light": [
    filled("circle", { cx: 12, cy: 12, r: 4.6 }),
    ...[0, 45, 90, 135, 180, 225, 270, 315].map(sunRay),
  ],
  // Theme "dark": crescent moon.
  "theme-dark": [filled("path", { d: "M20.5 15.2A9 9 0 0 1 8.8 3.5 9 9 0 1 0 20.5 15.2Z" })],
  // Screensaver: a framed photo (mountains and sun).
  picture: [
    stroked("rect", { x: 2.5, y: 4.5, width: 19, height: 15, rx: 2.5 }),
    filled("circle", { cx: 8, cy: 9.5, r: 1.9 }),
    stroked("path", { d: "M4 17 9.5 11.5 13.5 15.5 16.5 12.5 20 16.5" }),
  ],
  // Radar animation controls.
  play: [filled("polygon", { points: "8 4.5 19.5 12 8 19.5" })],
  pause: [
    filled("rect", { x: 7, y: 4.5, width: 3.8, height: 15, rx: 1.2 }),
    filled("rect", { x: 13.2, y: 4.5, width: 3.8, height: 15, rx: 1.2 }),
  ],
  // Admin gear: a THICK ring with eight square teeth. Deliberately not the
  // "disc plus thin rays" of the sun icon — the two sit next to each other
  // in the header and must not look alike at kiosk distance.
  gear: [
    { tag: "circle", attrs: { ...STROKE, cx: 12, cy: 12, r: 6.2, "stroke-width": 3.2 } },
    ...cogTeeth(),
  ],
};

/** The names `createIcon` accepts (exported for tests). */
export const ICON_NAMES = Object.keys(ICONS);

/**
 * A fresh <svg> element for `name`, or null for an unknown name (the caller
 * then leaves the slot empty rather than crashing the whole header).
 */
export function createIcon(name) {
  const shapes = ICONS[name];
  if (shapes === undefined) return null;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  // Decorative: the German name lives on the button's aria-label.
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const shape of shapes) {
    const node = document.createElementNS(SVG_NS, shape.tag);
    for (const [key, value] of Object.entries(shape.attrs)) {
      node.setAttribute(key, String(value));
    }
    svg.append(node);
  }
  return svg;
}

/** Put the icon named by `data-icon` into every such slot below `root`. */
export function renderIcons(root) {
  for (const slot of root.querySelectorAll("[data-icon]")) {
    setIcon(slot, slot.dataset.icon);
  }
}

/** Replace the icon inside one slot (used when a control changes state). */
export function setIcon(slot, name) {
  const icon = createIcon(name);
  slot.dataset.icon = name;
  if (icon === null) slot.replaceChildren();
  else slot.replaceChildren(icon);
}
