// Tiny DOM helpers. Rule 4 (CLAUDE.md): all strings coming from calendar
// data are rendered exclusively via textContent, never as markup.

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
