// The live departure board beside the slideshow (Etappe 52).
//
// Two narrow panels at the screen edges: left the departures towards
// Pasing, right the opposite direction. Which lines and directions those
// are is decided server-side (app/transit.py) — this module only draws
// what the board hands it, so a change of stops never touches the view.
//
// It lives INSIDE the slideshow overlay, as a sibling of the photo
// layers. That matters twice over: the panels are never caught by the
// photo rotation (same reasoning as the vertical text overlays), and
// they disappear with the slideshow instead of hanging over the
// calendar.
//
// Placement: on a portrait photo the panels sit in the black letterbox
// bars, which is where Roland asked for them. A landscape photo fills
// the width and leaves no bars — so they carry their own translucent
// backing and stay readable on top of the picture.

import { fetchDepartures } from "./api.js";
import { el } from "./dom.js";
import { badgeShape, departureLabel, lineColor } from "./transit-format.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// The server caches for 30 s; polling faster would only cost requests.
const REFRESH_MS = 30000;

let panels = null; // { left, right }
let timer = null;
let generation = 0;

/**
 * The line badge as inline SVG — never an image file and never an emoji
 * (the kiosk browser has no font for those; see icons.js). The S-Bahn
 * capsule and the squarer bus tile mirror the printed MVV artwork.
 */
function badge(kind, label) {
  const text = String(label ?? "");
  // Wide enough for "143" without stretching "S3" out of shape.
  const width = text.length >= 3 ? 52 : 42;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} 24`);
  svg.setAttribute("class", "transit-badge");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.style.width = `${width}px`;

  const plate = document.createElementNS(SVG_NS, "rect");
  plate.setAttribute("x", "0");
  plate.setAttribute("y", "0");
  plate.setAttribute("width", String(width));
  plate.setAttribute("height", "24");
  plate.setAttribute("rx", badgeShape(kind) === "capsule" ? "12" : "4");
  plate.setAttribute("fill", lineColor(kind, label));
  svg.append(plate);

  const caption = document.createElementNS(SVG_NS, "text");
  caption.setAttribute("x", String(width / 2));
  caption.setAttribute("y", "17");
  caption.setAttribute("text-anchor", "middle");
  caption.setAttribute("fill", "#ffffff");
  caption.setAttribute("font-size", "15");
  caption.setAttribute("font-weight", "600");
  caption.textContent = text;
  svg.append(caption);
  return svg;
}

function renderBlock(group) {
  const block = el("div", "transit-block");
  const head = el("div", "transit-head");
  head.append(badge(group.kind, group.label));
  // Foreign string from the server — textContent only.
  head.append(el("span", "transit-dest", group.towards ?? ""));
  block.append(head);

  const times = el("div", "transit-times");
  const departures = Array.isArray(group.departures) ? group.departures : [];
  if (departures.length === 0) {
    times.append(el("span", "transit-none", "keine Abfahrt"));
  }
  for (const departure of departures) {
    const label = departureLabel(departure);
    if (label === "") continue;
    const row = el("span", "transit-time", label);
    if (departure.cancelled) row.classList.add("transit-cancelled");
    times.append(row);
  }
  block.append(times);
  return block;
}

function renderBoard(board) {
  if (panels === null) return;
  for (const side of ["left", "right"]) {
    panels[side].replaceChildren();
  }
  const groups = Array.isArray(board?.groups) ? board.groups : [];
  for (const group of groups) {
    const panel = panels[group?.side === "right" ? "right" : "left"];
    panel.append(renderBlock(group));
  }
}

function renderError() {
  if (panels === null) return;
  // Only the left panel carries the message: saying it twice on one
  // screen is noise, and the right panel simply stays empty.
  panels.left.replaceChildren(el("p", "transit-error", "Abfahrten gerade nicht verfügbar."));
  panels.right.replaceChildren();
}

async function refresh() {
  const mine = generation;
  let board;
  try {
    board = await fetchDepartures();
  } catch {
    // A late answer from a previous slideshow run must not paint over
    // the current one (same guard as the radar preloads).
    if (mine === generation) renderError();
    return;
  }
  if (mine === generation) renderBoard(board);
}

/** Attach both panels to the slideshow overlay and start polling. */
export function startTransit(overlay) {
  stopTransit();
  generation += 1;
  const left = el("aside", "slideshow-transit slideshow-transit-left");
  const right = el("aside", "slideshow-transit slideshow-transit-right");
  // Decorative for assistive tech: the photos are the content here, and
  // a screen reader should not wade through a timetable to reach them.
  for (const panel of [left, right]) {
    panel.setAttribute("aria-hidden", "true");
    overlay.append(panel);
  }
  panels = { left, right };
  refresh();
  timer = setInterval(refresh, REFRESH_MS);
}

/** Stop polling and forget the panels (the overlay removes itself). */
export function stopTransit() {
  generation += 1;
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
  panels = null;
}
