// Backend access. All URLs are relative so the app works behind HA ingress.

export async function fetchEvents(fromISO, toISO) {
  const query = new URLSearchParams({ from: fromISO, to: toISO });
  const response = await fetch(`api/events?${query}`);
  if (!response.ok) {
    throw new Error(`Ereignisse laden fehlgeschlagen: HTTP ${response.status}`);
  }
  const payload = await response.json();
  return payload.events;
}

export async function fetchTags(fromISO, toISO) {
  const query = new URLSearchParams({ from: fromISO, to: toISO });
  const response = await fetch(`api/tags?${query}`);
  if (!response.ok) {
    throw new Error(`Symbole laden fehlgeschlagen: HTTP ${response.status}`);
  }
  const payload = await response.json();
  return payload.tags;
}

export async function fetchTagOptions() {
  const response = await fetch("api/tags/options");
  if (!response.ok) {
    throw new Error(`Symbolauswahl laden fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
}

export async function putDayTags(dateISO, emojis) {
  const response = await fetch(`api/tags/${dateISO}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ emojis }),
  });
  if (!response.ok) {
    throw new Error(`Symbole speichern fehlgeschlagen: HTTP ${response.status}`);
  }
  const payload = await response.json();
  return payload.emojis;
}
