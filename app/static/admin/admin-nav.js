// Tab shell of the admin page (Etappe 51, step 1).
//
// The page had grown to eleven sections on one endless scroll. They are now
// grouped by what Roland is doing — sources, syncs, display, review — rather
// than by the technology behind them.
//
// WHY A <nav> WITH REAL LINKS AND NOT AN ARIA TABLIST:
// an ARIA `tablist` obliges us to implement a roving tabindex, arrow-key
// navigation, aria-controls wiring and focus management — a small state
// machine that is easy to get subtly wrong and impossible to see when it is.
// A <nav> landmark with ordinary <a href="#..."> links plus `aria-current` on
// the active one is fully operable with Tab and Enter, is announced as a
// navigation landmark, and has no state machine at all. Switching therefore
// runs over `location.hash` and a `hashchange` listener, with NO pushState and
// NO preventDefault: the back button, link sharing and surviving a reload then
// come for free instead of being rebuilt.
//
// WHY IT IS PROGRESSIVE ENHANCEMENT:
// every section stays in the document and is visible by default — exactly as
// before. Only this module sets `document.body.dataset.tabs = "on"`, and only
// then does the CSS rule that hides inactive panels start to apply. If this
// module fails to load or throws, Roland sees the long page he has today —
// NEVER an empty one. That property matters more than the tabs themselves and
// is nailed down by an E2E test that loads the page without JavaScript.
//
// WHY THE TAB OF A SECTION IS LOOKED UP IN THE DOM:
// the mapping "section id → tab" is derived by walking up from the element to
// its enclosing `.admin-panel` instead of being kept as a second list here.
// A hardcoded table would be a second source of truth that silently rots the
// moment a section is moved between tabs (step 3 will move several).

export const DEFAULT_TAB = "quellen";

// Order and wording of the tab bar; the panels are `tab-<hash>` in admin.html.
export const TABS = [
  { hash: "quellen", label: "Quellen" },
  { hash: "syncs", label: "Syncs & Abo" },
  { hash: "anzeige", label: "Anzeige" },
  { hash: "durchsicht", label: "Durchsicht" },
];

const TAB_HASHES = new Set(TABS.map((tab) => tab.hash));

// "#busy-heading" -> "busy-heading". Percent escapes are decoded because
// location.hash hands them over encoded; anything undecodable (a lone "%")
// is kept verbatim rather than throwing.
export function normalizeHash(raw) {
  if (typeof raw !== "string") return "";
  const bare = raw.startsWith("#") ? raw.slice(1) : raw;
  try {
    return decodeURIComponent(bare);
  } catch {
    return bare;
  }
}

// Decide which tab a hash means and whether to scroll to a section inside it.
// `panelOf(id)` answers "which .admin-panel contains the element with this
// id?" — injected so this stays a pure, node-testable function.
export function resolveTarget(raw, panelOf) {
  const fallback = { tab: DEFAULT_TAB, scrollTo: null };
  const id = normalizeHash(raw);
  if (id === "") return fallback;
  if (TAB_HASHES.has(id)) return { tab: id, scrollTo: null };
  let panelId = null;
  try {
    panelId = panelOf(id);
  } catch {
    // A deep link into a section that no longer exists must never break the
    // page — it just lands on the default tab.
    return fallback;
  }
  if (typeof panelId !== "string" || !panelId.startsWith("tab-")) return fallback;
  const hash = panelId.slice("tab-".length);
  if (!TAB_HASHES.has(hash)) return fallback;
  return { tab: hash, scrollTo: id };
}

export function initAdminNav() {
  const nav = document.getElementById("admin-tabs");
  if (!nav) return;

  const links = new Map();
  for (const link of nav.querySelectorAll("a[data-tab]")) links.set(link.dataset.tab, link);
  const panels = document.querySelectorAll(".admin-panel");
  if (links.size === 0 || panels.length === 0) return;

  // Only from here on does the CSS hide anything — see the module header.
  document.body.dataset.tabs = "on";

  let current = null;
  const panelOf = (id) => document.getElementById(id)?.closest(".admin-panel")?.id ?? null;

  const apply = () => {
    const { tab, scrollTo } = resolveTarget(window.location.hash, panelOf);
    for (const [hash, link] of links) {
      const active = hash === tab;
      link.classList.toggle("admin-tab-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    for (const panel of panels) {
      panel.classList.toggle("admin-panel-active", panel.id === `tab-${tab}`);
    }
    const changed = tab !== current;
    current = tab;
    if (scrollTo) {
      // The panel was display:none a moment ago, so the browser's own jump to
      // the fragment did nothing — scroll-margin-top keeps the heading clear
      // of the sticky bar.
      document.getElementById(scrollTo)?.scrollIntoView();
    } else if (changed) {
      window.scrollTo(0, 0);
    }
  };

  window.addEventListener("hashchange", apply);
  apply();
}
