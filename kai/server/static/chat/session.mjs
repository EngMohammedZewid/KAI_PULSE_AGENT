// KAI Pulse session: the API token, the current conversation and calls to the KAI Pulse server.
// The token is the only thing kept in the browser (as the old chat page did).
const TOKEN_KEY = "kai_token";
const store = (value) => {
  try {
    if (value === undefined) return localStorage.getItem(TOKEN_KEY) || "";
    if (value) localStorage.setItem(TOKEN_KEY, value); else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage can be blocked; the token then lasts for this page view */ }
  return value || "";
};

const listeners = new Set();
export const session = { token: store(), thread: null, user: null };
export const onSessionChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
const changed = () => listeners.forEach((fn) => fn(session));

export class SignedOut extends Error {}

/** Call the KAI Pulse API. `json` sends a JSON body; a FormData `body` is sent as is. */
export async function api(path, { method = "GET", json, body, signal } = {}) {
  const headers = { Authorization: `Bearer ${session.token}` };
  if (json !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method, headers, signal, body: json !== undefined ? JSON.stringify(json) : body });
  if (response.status === 401) { signOut(); throw new SignedOut("signed out"); }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`);
  return data;
}

/** Check a token with the server; keep it only if it works. */
export async function signIn(token) {
  const value = String(token || "").trim();
  if (!value) return false;
  const response = await fetch("/api/me", { headers: { Authorization: `Bearer ${value}` } });
  if (!response.ok) return false;
  session.token = store(value);
  session.user = await response.json();
  session.thread = null;
  changed();
  return true;
}

export function signOut() {
  session.token = store("");
  session.user = null;
  session.thread = null;
  changed();
}

export const newConversation = () => { session.thread = null; };

/** Thumbs up or down on a reply; `index` is the server's message index. */
export async function rate(index, value) {
  if (!session.thread || !Number.isInteger(index)) return;
  await api(`/api/threads/${session.thread}/feedback`, { method: "POST", json: { message_index: index, rating: value } });
}
