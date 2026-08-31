const API_ROOT = "/api";

const storage = sessionStorage;

export function getToken() { return storage.getItem("meeting_token"); }
export function getStoredUser() {
  try { return JSON.parse(storage.getItem("meeting_user") || "null"); }
  catch (_) { storage.removeItem("meeting_user"); return null; }
}
export function saveSession(payload) {
  storage.setItem("meeting_token", payload.access_token);
  storage.setItem("meeting_user", JSON.stringify(payload.user));
}
export function clearSession() {
  storage.removeItem("meeting_token");
  storage.removeItem("meeting_user");
}
function requestId() {
  return globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Request-ID", requestId());
  if (getToken()) headers.set("Authorization", `Bearer ${getToken()}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_ROOT}${path}`, { ...options, headers });
  if (response.status === 401 && getToken()) {
    clearSession();
    location.replace("/login.html");
    throw new Error("نشست شما منقضی شده است.");
  }
  if (!response.ok) {
    let message = `خطای ${response.status}`;
    try {
      const payload = await response.json();
      message = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join("، ") : payload.detail || message;
    } catch (_) { /* non-JSON error */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
