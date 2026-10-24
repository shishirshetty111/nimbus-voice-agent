// API keys panel. Keys live only in this browser's localStorage and are sent to the
// Nimbus backend as X-Api-Key-<provider> headers on each request that needs them.
// When a key is blank, a local dev backend falls back to its own .env key (it reports only
// whether one exists, never the value). The production site never does: visitors bring their own.

import { el, escapeHtml } from "../app.js";

const STORE = "nimbus_api_keys";

export const PROVIDERS = [
  { id: "openai", label: "OpenAI", placeholder: "sk-...", uses: "ASR, LLM, TTS, embeddings" },
  { id: "anthropic", label: "Anthropic", placeholder: "sk-ant-...", uses: "LLM" },
  { id: "gemini", label: "Google Gemini", placeholder: "AIza...", uses: "ASR, LLM, TTS" },
  { id: "elevenlabs", label: "ElevenLabs", placeholder: "sk_...", uses: "ASR, TTS" },
];

export function loadKeys() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

function saveKeys(keys) {
  try {
    localStorage.setItem(STORE, JSON.stringify(keys));
    return true;
  } catch {
    return false; // private mode / storage blocked: keys still work for this page view
  }
}

let memoryKeys = null; // fallback when localStorage is unavailable
const currentKeys = () => memoryKeys || loadKeys();

// Save (or, with an empty value, remove) one provider's key.
export function setKey(provider, value) {
  const all = { ...currentKeys(), [provider]: (value || "").trim() };
  if (!all[provider]) delete all[provider];
  if (!saveKeys(all)) memoryKeys = all;
}

export function keyHeaders() {
  const out = {};
  for (const [p, v] of Object.entries(currentKeys())) if (v) out[`X-Api-Key-${p}`] = v;
  return out;
}

// Mount the panel. serverStatus = {openai: true, ...} from /api/keys/status (or null if offline).
export function mountKeysPanel(container, serverStatus = null) {
  const keys = currentKeys();
  const anySaved = PROVIDERS.some((p) => keys[p.id]);
  const panel = el("details", { class: "settings-panel keys-panel" });
  if (!anySaved) panel.open = true;
  panel.append(
    el("summary", {
      html: `<span>API keys <span class="panel-sub">stored in this browser only</span></span><span class="keys-count"></span>`,
    })
  );
  const body = el("div", { class: "keys-body" });
  panel.append(body);

  const rows = PROVIDERS.map((p) => {
    const input = el("input", {
      class: "inp mono",
      type: "password",
      placeholder: p.placeholder,
      autocomplete: "off",
      spellcheck: "false",
      "aria-label": `${p.label} API key`,
    });
    input.value = keys[p.id] || "";
    const status = el("span", { class: "key-status" });
    const show = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Show");
    const clear = el("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Clear");

    const paint = () => {
      const has = !!input.value.trim();
      const server = serverStatus && serverStatus[p.id];
      status.className = "key-status " + (has ? "ok" : server ? "server" : "none");
      status.textContent = has ? "Saved in browser" : server ? "Using server .env key" : "Not set";
      show.disabled = !has;
    };
    const persist = () => {
      setKey(p.id, input.value);
      paint();
      count();
    };
    input.addEventListener("input", persist);
    show.addEventListener("click", () => {
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      show.textContent = hidden ? "Hide" : "Show";
    });
    clear.addEventListener("click", () => {
      input.value = "";
      input.type = "password";
      show.textContent = "Show";
      persist();
    });
    paint();
    return el("div", { class: "key-row" }, [
      el("div", { class: "key-name", html: `<strong>${escapeHtml(p.label)}</strong><em>${escapeHtml(p.uses)}</em>` }),
      input,
      el("div", { class: "key-actions" }, [show, clear]),
      status,
    ]);
  });
  rows.forEach((r) => body.append(r));
  body.append(
    el("p", {
      class: "field-hint keys-note",
      html:
        "Keys are kept in this browser's <code>localStorage</code> and sent only to the Nimbus backend with each request. " +
        "They are never stored on the server. Anyone with access to this browser profile can read them, so use this on your own machine.",
    })
  );

  const countEl = panel.querySelector(".keys-count");
  function count() {
    const n = PROVIDERS.filter((p) => currentKeys()[p.id]).length;
    countEl.textContent = `${n} of ${PROVIDERS.length} saved`;
  }
  count();
  container.append(panel);
  return panel;
}
