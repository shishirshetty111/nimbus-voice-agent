// Playground settings, kept per viewer in localStorage. Later milestones (LLM, ASR, TTS) add keys.

const STORE = "nimbus_playground";

export const DEFAULTS = {
  tools_enabled: null, // null = all tools; [] = none; otherwise the enabled tool names
  // LLM layer (M4). Missing values fall back to the backend's defaults from /api/chat/config.
  provider: null,
  model: null,
  persona: "",
  length: null,
  verbatim_turns: null,
  knowledge_mode: null,
  rag_k: null,
  rerank: null,
  // Voice (M5)
  voice_mode: null,     // "streaming" | "batch"
  asr_provider: null,
  tts_provider: null,
  tts_voice: null,
  endpoint_ms: null,
  buffer_ms: null,
  barge_in: null,      // M6: talk over the agent to interrupt it
  latency_mode: null,  // "fast" = Low-latency mode (see docs/latency-reduction-plan.md)
};

export function getSettings() {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(STORE) || "{}") || {}) };
  } catch {
    return { ...DEFAULTS };
  }
}

export function setSettings(patch) {
  const next = { ...getSettings(), ...patch };
  try {
    localStorage.setItem(STORE, JSON.stringify(next));
  } catch {}
  return next;
}
