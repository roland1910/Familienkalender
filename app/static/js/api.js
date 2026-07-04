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

export async function fetchPower() {
  const response = await fetch("api/power");
  if (!response.ok) {
    // The backend sends German error details (e.g. "Home Assistant ist
    // nicht erreichbar."); surface them in the view's error state.
    let detail = `Stromdaten laden fehlgeschlagen: HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // keep the generic message
    }
    throw new Error(detail);
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
