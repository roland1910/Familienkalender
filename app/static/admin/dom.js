// Shared DOM helpers for the admin modules. Foreign strings (source
// names, calendar names, error messages) go into the DOM exclusively
// via textContent.

export function byId(id) {
  return document.getElementById(id);
}

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function showMessage(node, text, isError) {
  node.textContent = text;
  node.hidden = !text;
  node.classList.toggle("error-text", Boolean(isError));
}

// Run an async action; failures land in the page-level message area.
export function withPageError(action) {
  return action().catch((error) => {
    showMessage(byId("page-message"), error.message, true);
  });
}
