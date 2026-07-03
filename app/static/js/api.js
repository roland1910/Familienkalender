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
