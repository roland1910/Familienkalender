// Admin backend access. All URLs are relative so the page works behind
// HA ingress (served at <ingress-base>/admin, APIs at <ingress-base>/api/...).

async function request(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
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

export function getSettings() {
  return request("GET", "api/admin/settings");
}

export function saveSettings(eveningBoundary) {
  return request("PUT", "api/admin/settings", { evening_boundary: eveningBoundary });
}

export function savePowerDevices(devices) {
  return request("PUT", "api/admin/settings/power", { devices });
}

export function saveGoogleCredentials(clientId, clientSecret) {
  return request("PUT", "api/admin/settings/google", {
    client_id: clientId,
    client_secret: clientSecret,
  });
}

export function getSources() {
  return request("GET", "api/admin/sources");
}

export function createSource(source) {
  return request("POST", "api/admin/sources", source);
}

export function patchSource(sourceId, changes) {
  return request("PATCH", `api/admin/sources/${sourceId}`, changes);
}

export function deleteSource(sourceId) {
  return request("DELETE", `api/admin/sources/${sourceId}`);
}

export function probeCaldav(url, username, appPassword) {
  return request("POST", "api/admin/caldav/calendars", {
    url,
    username,
    app_password: appPassword,
  });
}

export function googleAuthUrl() {
  return request("POST", "api/admin/google/auth-url");
}

export function googleConnect(code) {
  return request("POST", "api/admin/google/connect", { code });
}

export function deleteGooglePending(flowId) {
  return request("DELETE", `api/admin/google/pending/${encodeURIComponent(flowId)}`);
}

export function getFeed() {
  return request("GET", "api/admin/feed");
}

export function rotateFeed() {
  return request("POST", "api/admin/feed/rotate");
}

export function saveFeedHost(host) {
  return request("PUT", "api/admin/feed/host", { host });
}

export function triggerSync() {
  return request("POST", "api/sync");
}

export function getSlideshow() {
  return request("GET", "api/admin/slideshow");
}

export function saveSlideshowDirs(dirs) {
  return request("PUT", "api/admin/slideshow", { dirs });
}

export function rescanSlideshow() {
  return request("POST", "api/admin/slideshow/rescan");
}

export function listMediaDirs(path) {
  const query = new URLSearchParams({ path: path ?? "" });
  return request("GET", `api/admin/slideshow/dirs?${query}`);
}
