// Backend client. The FastAPI backend serves the site too (locally and on Vercel), so requests are
// same-origin. A developer can point a page at another backend with localStorage "nimbus_api_base".

import { getCartId } from "./cart-id.js";
import { keyHeaders } from "./keys.js";

// Every request carries the API keys from the keys panel and the shared cart id.
const baseHeaders = () => ({ "X-Cart-Id": getCartId(), ...keyHeaders() });

const BASE_STORE = "nimbus_api_base";

export function apiBase() {
  try {
    const saved = localStorage.getItem(BASE_STORE);
    if (saved) return saved.replace(/\/+$/, "");
  } catch {}
  return "";
}

export function setApiBase(url) {
  try {
    if (url) localStorage.setItem(BASE_STORE, url);
    else localStorage.removeItem(BASE_STORE);
  } catch {}
}

export async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(apiBase() + path, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...baseHeaders() },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error(`Cannot reach the backend at ${apiBase() || location.origin}. Check your connection and try again.`);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// POST and read an NDJSON stream; onEvent is called once per JSON line as it arrives.
export async function apiStream(path, body, onEvent) {
  let res;
  try {
    res = await fetch(apiBase() + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...baseHeaders() },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(`Cannot reach the backend at ${apiBase() || location.origin}.`);
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}
