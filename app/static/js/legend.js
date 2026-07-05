// Compact source legend below the calendar: one dot + name per enabled
// source, using the exact color mapping of the event chips (colors.js).

import { colorForSource } from "./colors.js";
import { el } from "./dom.js";

export function renderLegend(container, sources) {
  const enabled = sources.filter((source) => source.enabled);
  container.replaceChildren();
  container.hidden = enabled.length === 0;
  for (const source of enabled) {
    const item = el("span", "legend-item");
    const dot = el("span", "legend-dot");
    dot.style.setProperty("--source-color", colorForSource(source));
    // Source names come from admin input — textContent only (rule 4).
    item.append(dot, el("span", "legend-name", source.name));
    container.append(item);
  }
}
