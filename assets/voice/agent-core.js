// The finished Nimbus agent, shared by agent.html and the landing-page widget.
//
// It runs a fixed preset (the Playground is where settings are tuned). Each part uses the first
// provider in its preference list that has a key, in this browser (API keys panel) or on the server.
// Agent page and widget share one conversation and one cart.

import { api, apiStream } from "./api.js";
import { getCartId } from "./cart-id.js";
import { KEY_IN_CHAT, loadKeys, looksLikeKey } from "./keys.js";
import { fromTextTurn, fromVoiceTurn, storeTurn } from "./latency-model.js";
import { VoiceClient } from "./voice-client.js";

export const PRESET = {
  llm: [["openai", "gpt-6-luna"], ["gemini", "gemini-3.8-flash"], ["anthropic", "claude-haiku-4-5"]], // fastest of each
  asr: ["openai", "elevenlabs", "browser", "gemini"],
  tts: [["openai", "marin"], ["elevenlabs", null], ["gemini", "Kore"]],
  chat: { persona: "", length: "low", verbatim_turns: 6, knowledge_mode: "rag", rag_k: 5, rerank: false, tools_enabled: null },
  voice: { mode: "streaming", endpoint_ms: 600, buffer_ms: 120, barge_in: true },
};
const LABELS = { openai: "OpenAI", gemini: "Gemini", anthropic: "Anthropic", elevenlabs: "ElevenLabs", browser: "browser" };
const SESSION_KEY = "nimbus_agent_session";
const browserAsr = () => !!(window.SpeechRecognition || window.webkitSpeechRecognition);

function sessionId() {
  const make = () => "a_" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(36).slice(2) + Date.now().toString(36));
  try {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) localStorage.setItem(SESSION_KEY, (id = make()));
    return id;
  } catch {
    return (sessionId.mem ||= make());
  }
}

// The finished agent runs in Low-latency mode unless the visitor switched it off on agent.html.
const FAST_KEY = "nimbus_agent_fast";
export function agentFast() {
  try {
    return localStorage.getItem(FAST_KEY) !== "0";
  } catch {
    return true;
  }
}
export function setAgentFast(on) {
  try {
    localStorage.setItem(FAST_KEY, on ? "1" : "0");
  } catch {}
}

export function resolvePreset(serverKeys = {}) {
  const browser = loadKeys();
  const latency_mode = agentFast() ? "fast" : "normal";
  const has = (p) => p === "browser" ? browserAsr() : !!(browser[p] || serverKeys[p]);
  const llm = PRESET.llm.find(([p]) => has(p));
  const asr = PRESET.asr.find(has);
  const tts = PRESET.tts.find(([p]) => has(p));
  const chat = llm ? { ...PRESET.chat, provider: llm[0], model: llm[1], latency_mode } : null;
  // Developer hook: localStorage "nimbus_agent_override" = {"chat": {...}, "voice": {...}} replaces
  // parts of the preset (used to test against tests/fake_server.py without real keys).
  let o = {};
  try {
    o = JSON.parse(localStorage.getItem("nimbus_agent_override") || "{}") || {};
  } catch {}
  return {
    chat: chat && { ...chat, ...(o.chat || {}) },
    canText: !!llm,
    canVoice: !!(llm && asr && tts),
    voice: { ...PRESET.voice, asr_provider: asr, tts_provider: tts?.[0], tts_voice: tts?.[1], latency_mode, ...(o.voice || {}) },
    fast: latency_mode === "fast",
    summary: llm
      ? `${latency_mode === "fast" ? "⚡ Low-latency · " : ""}${LABELS[llm[0]]} ${llm[1]} · RAG · ${tts ? `voice: ${LABELS[asr] || "?"} → ${LABELS[tts[0]]}` : "text only (no speech key)"}`
      : "no API key yet",
  };
}

export class NimbusAgent {
  // view: {onState, onLevel, onPartial, onUser, onAgentStart, onAgentEvent, onInterrupted, onTurn, onCart, onError}
  constructor(view) {
    this.v = view;
    this.voice = null;
  }

  async init() {
    this.serverKeys = {};
    try {
      this.serverKeys = await api("/api/keys/status");
    } catch (err) {
      this.v.onError?.(err.message);
    }
    this.preset = resolvePreset(this.serverKeys);
    return this.preset;
  }

  // Switch Low-latency mode; a live voice session picks it up immediately.
  setFast(on) {
    setAgentFast(on);
    this.preset = resolvePreset(this.serverKeys);
    if (this.voiceOn) this.voice.update(this.startMsg(), this.preset.voice);
    return this.preset;
  }

  get voiceOn() {
    return !!this.voice && this.voice.state !== "off";
  }

  startMsg() {
    const p = this.preset.voice;
    return { type: "start", keys: loadKeys(), cart_id: getCartId(), session_id: sessionId(),
             settings: { mode: p.mode, asr_provider: p.asr_provider, tts_provider: p.tts_provider, tts_voice: p.tts_voice, chat: this.preset.chat } };
  }

  async toggleVoice() {
    if (this.voiceOn) {
      await this.voice.stop();
      this.voice = null;
      return false;
    }
    this.voice = new VoiceClient({
      onState: (s) => this.v.onState?.(s),
      onLevel: (x) => this.v.onLevel?.(x),
      onPartial: (t) => this.v.onPartial?.(t),
      onError: (m) => this.v.onError?.(m),
      onEvent: (ev) => this.onVoiceEvent(ev),
      onTurnDone: (t) => {
        const rec = fromVoiceTurn(t);
        storeTurn(rec);
        this.v.onTurn?.(rec, t);
      },
    });
    try {
      await this.voice.start(this.startMsg(), this.preset.voice);
      return true;
    } catch (err) {
      this.v.onError?.(err.message || String(err));
      await this.voice.stop();
      this.voice = null;
      return false;
    }
  }

  onVoiceEvent(ev) {
    if (ev.type === "asr_final") {
      this.v.onUser?.(ev.text);
      this.v.onAgentStart?.();
    } else if (["delta", "tool", "rag", "done", "error"].includes(ev.type)) {
      this.v.onAgentEvent?.(ev);
    } else if (ev.type === "interrupted") {
      this.v.onInterrupted?.(ev);
    } else if (ev.type === "barge_in") {
      this.v.onBargeIn?.(ev);
    } else if (ev.type === "cart_changed") {
      this.cartChanged();
    }
  }

  cartChanged() {
    window.dispatchEvent(new Event("nimbus:cart-changed"));
    this.v.onCart?.();
  }

  // Typed message: spoken back if voice is on, otherwise a text reply.
  async sendText(text) {
    if (looksLikeKey(text)) return this.v.onError?.(KEY_IN_CHAT);
    this.v.onUser?.(text);
    this.v.onAgentStart?.();
    if (this.voiceOn) return this.voice.sendText(text);
    try {
      await apiStream("/api/chat", { session_id: sessionId(), message: text, settings: this.preset.chat }, (ev) => {
        this.v.onAgentEvent?.(ev);
        if (ev.type === "tool" && ev.changes_cart) this.cartChanged();
        if (ev.type === "done") {
          const rec = fromTextTurn(ev);
          storeTurn(rec);
          this.v.onTurn?.(rec, ev);
        }
      });
    } catch (err) {
      this.v.onAgentEvent?.({ type: "error", message: err.message });
    }
  }

  async history() {
    try {
      return (await api(`/api/chat/${sessionId()}?verbatim=${PRESET.chat.verbatim_turns}`)).transcript;
    } catch {
      return [];
    }
  }

  async reset() {
    try {
      await api(`/api/chat/${sessionId()}/reset`, { method: "POST" });
    } catch {}
    try {
      localStorage.removeItem(SESSION_KEY);
    } catch {}
    sessionId.mem = null;
  }
}
