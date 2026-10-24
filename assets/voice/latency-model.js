// Latency model shared by the dashboard, the agent page and the landing widget: stage definitions,
// conversion of raw turn results into critical-path records, and the per-browser turn store.


export const STORE = "nimbus_latency";
export const MAX_TURNS = 200;
// Fixed stage -> color (dataviz reference palette, validated as adjacent pairs). Never re-ordered.
export const STAGES = [
  { key: "endpoint", label: "End of speech", color: "#2a78d6", hint: "silence waited before deciding you were done" },
  { key: "asr", label: "Speech recognition", color: "#eb6834", hint: "endpoint → final transcript" },
  { key: "rag", label: "Retrieval (RAG)", color: "#1baf7a", hint: "FAISS search, automatic or via the search tool" },
  { key: "llm", label: "LLM", color: "#eda100", hint: "model time until the first speakable sentence" },
  { key: "tools", label: "Tools", color: "#e87ba4", hint: "cart / pricing / catalog tools" },
  { key: "tts", label: "Speech synthesis", color: "#008300", hint: "first sentence sent → first audio byte" },
  { key: "network", label: "Network", color: "#4a3aa7", hint: "server → browser" },
  { key: "buffer", label: "Playback buffer", color: "#e34948", hint: "audio held before playing" },
  { key: "other", label: "Other", color: "#9a9890", hint: "time the stages above don't account for" },
];
export const DARK_TEXT = new Set(["#1baf7a", "#eda100", "#e87ba4"]); // below 3:1 with white text
export const fmt = (ms) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);
export const pos = (x) => Math.max(0, Number(x) || 0);

// ------------------------------------------------------------------ records

export function loadTurns() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}
export function saveTurns(turns) {
  try {
    localStorage.setItem(STORE, JSON.stringify(turns.slice(-MAX_TURNS)));
  } catch {}
}

// Append one record to the shared store (used by pages that don't show the dashboard).
export function storeTurn(rec) {
  if (rec) saveTurns([...loadTurns(), rec]);
}

export function finish(rec) {
  const known = STAGES.filter((s) => s.key !== "other").reduce((a, s) => a + pos(rec.stages[s.key]), 0);
  rec.stages.other = Math.max(0, rec.total - known);
  return rec;
}

// Voice turn: server stage durations + client marks (all on the browser clock).
export function fromVoiceTurn(t) {
  const s = t.timings_ms, c = t.client;
  if (c.total == null) return null; // nothing was played
  const think = pos((s.tts_wait ?? s.server_first_audio) - pos(s.asr));
  const rag = pos(s.rag), tools = pos(s.tools_only);
  return finish({
    at: Date.now(), kind: "voice", mode: t.mode, knowledge: t.knowledge, profile: t.profile || "normal",
    providers: t.providers, total: c.total, filler: c.filler,
    stages: { endpoint: pos(s.endpoint), asr: pos(s.asr), rag, llm: pos(think - rag - tools), tools, tts: pos(s.tts_ttfb),
              network: pos(c.network), buffer: pos(c.buffer) },
    underruns: c.underruns, tokens: t.usage,
  });
}

// Typed chat turn: time to the first word of the reply.
export function fromTextTurn(ev) {
  const t = ev.timings_ms;
  if (t.first_word == null) return null;
  const rag = pos(t.rag), tools = pos(t.tools_only);
  return finish({
    at: Date.now(), kind: "text", mode: "text", knowledge: ev.knowledge, profile: ev.profile || "normal", total: t.first_word,
    providers: { llm: `${ev.provider}/${ev.model}` },
    stages: { rag, llm: pos(t.first_word - rag - tools), tools }, tokens: ev.usage,
  });
}

