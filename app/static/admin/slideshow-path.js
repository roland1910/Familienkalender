// Pure path helpers for the navigable slideshow directory browser. Kept
// framework-free and DOM-free so they can be node-unit-tested. All paths
// are the absolute, server-validated strings the backend returns; these
// helpers only slice/label them for display and never widen access — the
// media root stays the hard boundary (segments never reach above it).

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
