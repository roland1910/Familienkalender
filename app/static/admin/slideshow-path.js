// Pure helpers for the slideshow admin section (directory browser paths and
// the scan-availability warning text). Kept
// framework-free and DOM-free so they can be node-unit-tested. All paths
// are the absolute, server-validated strings the backend returns; these
// helpers only slice/label them for display and never widen access — the
// media root stays the hard boundary (segments never reach above it).

// German warning line for the last scan, or "" when everything was
// reachable. ``unavailable_dirs`` counts configured directories the scan
// could not read (typically: the CIFS share was not mounted yet);
// ``scan_skipped`` means none of them were reachable, so the index was left
// untouched instead of being wiped. Text only — the caller sets it via
// textContent and hides the element on "".
export function scanWarningText({ unavailable_dirs, scan_skipped } = {}) {
  const count = Number(unavailable_dirs) || 0;
  if (count <= 0) return "";
  const folders = count === 1 ? "1 Ordner war" : `${count} Ordner waren`;
  const tail = scan_skipped
    ? "der Index wurde deshalb nicht verändert."
    : "deren Fotos wurden unverändert im Index behalten.";
  return `Hinweis: ${folders} beim letzten Einlesen nicht erreichbar (Netzwerkfreigabe?) — ${tail}`;
}

// The display label for a directory path: its last segment, or the whole
// path if it has none (e.g. the drive/root itself).
export function shortName(path) {
  const parts = String(path).split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

// Breadcrumb segments from the media root down to (and including) the
// current path. Each segment is {name, path}: the root is labelled
// "/media" (its last segment), every deeper level its own name. Returns a
// single root segment when base === root. If base is not below root (should
// not happen — the backend validates), only the root segment is returned so
// the UI can never offer a path outside the share.
export function breadcrumbSegments(root, base) {
  // Normalize away a trailing slash so the root segment path is canonical.
  const rootPath = String(root).replace(/\/+$/, "") || "/";
  const rootName = shortName(rootPath);
  const segments = [{ name: rootName, path: rootPath }];
  const basePath = String(base).replace(/\/+$/, "") || "/";
  if (basePath === rootPath) return segments;
  // Only descend when base is genuinely below root.
  const prefix = rootPath === "/" ? "/" : `${rootPath}/`;
  if (!basePath.startsWith(prefix)) return segments;
  const rest = basePath.slice(prefix.length).split("/").filter(Boolean);
  let acc = rootPath;
  for (const name of rest) {
    acc = `${acc}/${name}`;
    segments.push({ name, path: acc });
  }
  return segments;
}
