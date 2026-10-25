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

export async function fetchSources() {
  const response = await fetch("api/sources");
  if (!response.ok) {
    throw new Error(`Quellen laden fehlgeschlagen: HTTP ${response.status}`);
  }
  const payload = await response.json();
  return payload.sources;
}

export async function fetchConfig() {
  // Belt and braces to the server-side no-store on /api/*: the kiosk once
  // answered this request from its heuristic cache with a stale payload.
  const response = await fetch("api/config", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Konfiguration laden fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
}

export async function fetchMe() {
  const response = await fetch("api/me");
  if (!response.ok) {
    throw new Error(`Benutzerstatus laden fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
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

export async function fetchPowerHistory(hours) {
  const query = new URLSearchParams({ hours: String(hours) });
  const response = await fetch(`api/power/history?${query}`);
  if (!response.ok) {
    let detail = `Verlaufsdaten laden fehlgeschlagen: HTTP ${response.status}`;
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

// -- weather view -----------------------------------------------------------

// The backend answers with a German detail message (e.g. "Der Wetterdienst
// ist nicht erreichbar."); surface it in the view's error state.
async function germanDetail(response, fallback) {
  try {
    const payload = await response.json();
    if (typeof payload.detail === "string") return payload.detail;
  } catch {
    // keep the generic message
  }
  return fallback;
}

export async function fetchWeatherForecast() {
  const response = await fetch("api/weather/forecast");
  if (!response.ok) {
    throw new Error(
      await germanDetail(response, `Vorhersage laden fehlgeschlagen: HTTP ${response.status}`),
    );
  }
  return response.json();
}

export async function fetchRadarFrames() {
  const response = await fetch("api/weather/radar/frames");
  if (!response.ok) {
    throw new Error(
      await germanDetail(response, `Regenradar laden fehlgeschlagen: HTTP ${response.status}`),
    );
  }
  const payload = await response.json();
  return payload.frames;
}

// Relative tile URLs (ingress!). All four segments are numbers computed in
// weather-map.js, never user input; they are encoded anyway so a future
// caller cannot smuggle path segments in.
export function baseTileUrl(z, x, y) {
  return `api/weather/tile/base/${encodeURIComponent(z)}/${encodeURIComponent(x)}/${encodeURIComponent(y)}`;
}

export function radarTileUrl(frame, z, x, y) {
  return `api/weather/tile/radar/${encodeURIComponent(frame)}/${baseTileSuffix(z, x, y)}`;
}

function baseTileSuffix(z, x, y) {
  return `${encodeURIComponent(z)}/${encodeURIComponent(x)}/${encodeURIComponent(y)}`;
}

// -- groundwater view -------------------------------------------------------

export async function fetchGroundwaterStations() {
  const response = await fetch("api/groundwater/stations");
  if (!response.ok) {
    throw new Error(
      await germanDetail(response, `Messstellen laden fehlgeschlagen: HTTP ${response.status}`),
    );
  }
  const payload = await response.json();
  return payload.stations;
}

// One collected answer for all marker curves — asking per station would be
// ~165 requests every time the view opens (see app/groundwater.py).
export async function fetchGroundwaterSparklines(days) {
  const query = new URLSearchParams({ days: String(days) });
  const response = await fetch(`api/groundwater/sparklines?${query}`);
  if (!response.ok) {
    throw new Error(
      await germanDetail(response, `Verlaufsdaten laden fehlgeschlagen: HTTP ${response.status}`),
    );
  }
  const payload = await response.json();
  return payload.series;
}

// The number comes from our own station list, never from user input; it is
// encoded anyway so no caller can smuggle a path segment in.
export async function fetchGroundwaterHistory(number) {
  const response = await fetch(`api/groundwater/history/${encodeURIComponent(number)}`);
  if (!response.ok) {
    throw new Error(
      await germanDetail(response, `Verlauf laden fehlgeschlagen: HTTP ${response.status}`),
    );
  }
  const payload = await response.json();
  return payload.points;
}

export async function fetchNextPhoto() {
  const response = await fetch("api/slideshow/next");
  if (!response.ok) {
    throw new Error(`Nächstes Foto laden fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
}

// Relative image URL for a photo id (works behind HA ingress). The id is a
// number from /api/slideshow/next, never a client-supplied path.
export function photoImageUrl(id) {
  return `api/slideshow/image/${encodeURIComponent(id)}`;
}

// Turn the photo a quarter in the given direction ("cw"/"ccw"). The answer
// carries the new ABSOLUTE angle — the view applies that rather than
// counting along itself, so a lost or doubled request cannot drift.
export async function rotatePhoto(id, direction) {
  const response = await fetch(`api/slideshow/rotate/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ direction }),
  });
  if (!response.ok) {
    throw new Error(`Foto drehen fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
}

// Flag the photo for deletion. Nothing is deleted here — the file only
// leaves the rotation and shows up in the admin list for a final decision.
export async function markPhotoForDeletion(id) {
  const response = await fetch(`api/slideshow/mark-delete/${encodeURIComponent(id)}`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(`Vormerken fehlgeschlagen: HTTP ${response.status}`);
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

// Live departure board for the slideshow (Etappe 52). A failure here is
// never fatal — the caller shows a hint and the photos keep running.
export async function fetchDepartures() {
  const response = await fetch("api/transit/departures");
  if (!response.ok) {
    throw new Error(`Abfahrten laden fehlgeschlagen: HTTP ${response.status}`);
  }
  return response.json();
}
