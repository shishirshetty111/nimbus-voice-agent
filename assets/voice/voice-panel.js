// Playground: voice. ASR + TTS providers, streaming vs batch, endpoint and playback-buffer sliders,
// the mic, and a per-turn latency breakdown. Turns land in the shared chat log and memory.

import { el, escapeHtml, icon } from "../app.js";
import { api } from "./api.js";
import { getCartId } from "./cart-id.js";
import { agentTurn, chatSessionId, chatSettings, userBubble } from "./chat-panel.js";
import { loadKeys } from "./keys.js";
import { getSettings, setSettings } from "./settings.js";
import { VoiceClient } from "./voice-client.js";

const $ = (sel, root = document) => root.querySelector(sel);
const STATES = {
  off: ["", "Click the mic to start talking"],
  connecting: ["dot-thinking", "Connecting…"],
  listening: ["dot-listening", "Listening…"],
  hearing: ["dot-listening", "Hearing you…"],
  thinking: ["dot-thinking", "Thinking…"],
  speaking: ["dot-speaking", "Speaking…"],
};
let cfg, client, view, cutView;

function vs() {
  const s = getSettings(), d = cfg.defaults;
  const tts = s.tts_provider && cfg.tts[s.tts_provider] ? s.tts_provider : d.tts_provider;
  return {
    mode: s.voice_mode || d.mode,
    asr_provider: s.asr_provider && cfg.asr[s.asr_provider] ? s.asr_provider : d.asr_provider,
    tts_provider: tts,
    tts_voice: s.tts_voice && (s.tts_provider === tts) ? s.tts_voice : cfg.tts[tts].default_voice,
    endpoint_ms: s.endpoint_ms ?? 700,
    buffer_ms: s.buffer_ms ?? 120,
    barge_in: s.barge_in ?? true,
    latency_mode: s.latency_mode === "fast" ? "fast" : "normal",
  };
}

const startMsg = () => {
  const v = vs();
  return {
    type: "start", keys: loadKeys(), cart_id: getCartId(), session_id: chatSessionId(),
    settings: { mode: v.mode, asr_provider: v.asr_provider, tts_provider: v.tts_provider, tts_voice: v.tts_voice, chat: chatSettings() },
  };
};

// ------------------------------------------------------------------ panel

export function voicePanel() {
  const field = (label, ...kids) => el("div", { class: "field" }, [el("span", { class: "field-label" }, label), ...kids]);
  const range = (id, min, max, step) =>
    el("div", { class: "range-wrap" }, [el("input", { type: "range", class: "inp-range", id, min, max, step }), el("span", { class: "range-val", id: `${id}-val` })]);
  return el("div", { class: "panel" }, [
    el("div", { class: "panel-h" }, ["Voice ", el("span", { class: "panel-sub" }, "speech in, speech out · uses the agent brain above")]),
    el("label", { class: "fast-toggle", id: "v-fast-wrap" }, [
      el("input", { type: "checkbox", id: "v-fast" }),
      el("span", { class: "fast-switch", "aria-hidden": "true" }),
      el("span", {}, [el("strong", {}, "⚡ Low-latency mode"),
        el("em", { id: "v-fast-hint" }, "Runs the whole pipeline with every latency reduction on (applies to typed chat too).")]),
    ]),
    el("div", { class: "brain-grid" }, [
      field("Speech recognition (ASR)", el("select", { class: "inp", id: "v-asr" }), el("span", { class: "field-hint", id: "v-asr-hint" })),
      field("Pipeline", el("div", { class: "seg", role: "radiogroup" }, [["streaming", "Streaming"], ["batch", "Batch"]].map(([v, l]) =>
        el("label", {}, [el("input", { type: "radio", name: "v-mode", value: v }), el("span", {}, l)]))),
        el("span", { class: "field-hint", id: "v-mode-hint" })),
      field("Speech synthesis (TTS)", el("select", { class: "inp", id: "v-tts" }), el("span", { class: "field-hint", id: "v-tts-hint" })),
      field("Voice", el("select", { class: "inp", id: "v-voice" }), el("input", { class: "inp mono", id: "v-voice-id", placeholder: "or paste an ElevenLabs voice id", hidden: "" })),
      field("End of speech after silence of", range("v-endpoint", 200, 1500, 50), el("span", { class: "field-hint" }, "Shorter replies faster but may cut you off mid-pause.")),
      field("Playback buffer", range("v-buffer", 0, 500, 10), el("span", { class: "field-hint" }, "Audio held before playing. 0 is fastest but can stutter on a slow network.")),
      field("Interruptions", el("label", { class: "check" }, [el("input", { type: "checkbox", id: "v-barge" }), " Let me talk over the agent (barge-in)"]),
        el("span", { class: "field-hint" }, "Speaking while it talks stops the audio at once; it remembers only what you heard.")),
    ]),
    el("div", { class: "talk-controls" }, [
      el("button", { class: "mic-btn", type: "button", id: "v-mic", "aria-label": "Start talking", html: icon("mic") }),
      el("div", { class: "voice-status" }, [
        el("div", { class: "talk-status", id: "v-status" }, [el("span", {}), el("span", {}, STATES.off[1])]),
        el("div", { class: "viz-level" }, [el("i", { id: "v-level", style: "width:0%" })]),
      ]),
    ]),
    el("div", { class: "asr-out", id: "v-partial" }, [el("span", { class: "asr-interim" }, "Your words appear here as you speak.")]),
    el("div", { id: "v-turn" }),
    el("p", { class: "field-hint" }, "Headphones work best: with speakers, the agent's own voice can leak into the mic. If it keeps interrupting itself, turn barge-in off or lower your volume."),
  ]);
}

export async function setupVoice() {
  cfg = await api("/api/voice/config");
  const v = vs();
  $("#v-asr").innerHTML = Object.entries(cfg.asr).map(([k, a]) => `<option value="${k}">${escapeHtml(a.label)}</option>`).join("");
  $("#v-tts").innerHTML = Object.entries(cfg.tts).map(([k, t]) => `<option value="${k}">${escapeHtml(t.label)}</option>`).join("");
  $("#v-asr").value = v.asr_provider;
  $("#v-tts").value = v.tts_provider;
  document.querySelector(`input[name="v-mode"][value="${v.mode}"]`).checked = true;
  $("#v-endpoint").value = v.endpoint_ms;
  $("#v-buffer").value = v.buffer_ms;
  $("#v-barge").checked = v.barge_in;
  $("#v-fast").checked = v.latency_mode === "fast";
  paintVoices();
  paintHints();

  const changed = (patch) => {
    setSettings(patch);
    paintHints();
    client?.update(startMsg(), vs());
  };
  $("#v-asr").addEventListener("change", () => changed({ asr_provider: $("#v-asr").value }));
  $("#v-tts").addEventListener("change", () => {
    setSettings({ tts_provider: $("#v-tts").value, tts_voice: null });
    paintVoices();
    changed({});
  });
  $("#v-voice").addEventListener("change", () => changed({ tts_voice: $("#v-voice").value }));
  $("#v-voice-id").addEventListener("change", () => changed({ tts_voice: $("#v-voice-id").value.trim() || null }));
  document.querySelectorAll('input[name="v-mode"]').forEach((r) => r.addEventListener("change", () => changed({ voice_mode: r.value })));
  $("#v-endpoint").addEventListener("input", () => changed({ endpoint_ms: +$("#v-endpoint").value }));
  $("#v-buffer").addEventListener("input", () => changed({ buffer_ms: +$("#v-buffer").value }));
  $("#v-barge").addEventListener("change", () => changed({ barge_in: $("#v-barge").checked }));
  $("#v-fast").addEventListener("change", () => {
    changed({ latency_mode: $("#v-fast").checked ? "fast" : "normal" });
    window.dispatchEvent(new Event("nimbus:tools-changed")); // refresh the rules preview
  });
  // Keep the voice session in step with the agent-brain settings and keys.
  ["change", "input"].forEach((t) => document.querySelector(".brain-grid")?.addEventListener(t, () => client?.update(startMsg(), vs())));
  $("#v-mic").addEventListener("click", toggle);
}

function paintVoices() {
  const v = vs(), t = cfg.tts[v.tts_provider];
  $("#v-voice").innerHTML = t.voices.map((x) => `<option value="${x}">${escapeHtml(x === t.default_voice ? `${x} (default)` : x)}</option>`).join("");
  $("#v-voice").value = t.voices.includes(v.tts_voice) ? v.tts_voice : t.default_voice;
  $("#v-voice-id").hidden = v.tts_provider !== "elevenlabs";
  $("#v-voice-id").value = v.tts_provider === "elevenlabs" && !t.voices.includes(v.tts_voice) ? v.tts_voice : "";
}

function paintHints() {
  const v = vs();
  const fast = v.latency_mode === "fast";
  $("#v-endpoint-val").textContent = fast ? "auto 500–1000" : `${v.endpoint_ms} ms`;
  $("#v-buffer-val").textContent = fast ? "auto 40+" : `${v.buffer_ms} ms`;
  $("#v-endpoint").disabled = $("#v-buffer").disabled = fast;
  $("#v-fast-wrap").classList.toggle("on", fast);
  $("#v-fast-hint").textContent = fast
    ? "On: lite model, auto-retrieval (k=3, no rerank) instead of the search tool, short answer-first replies, only the tools each message needs, adaptive end-of-speech, first-clause speech, 2 sentences synthesized ahead, warm ASR socket, 40 ms adaptive buffer, fillers when a reply is slow."
    : "Runs the whole pipeline with every latency reduction on (applies to typed chat too).";
  $("#v-asr-hint").textContent = cfg.asr[v.asr_provider][v.mode];
  $("#v-tts-hint").textContent = cfg.tts[v.tts_provider].model + (v.mode === "streaming" ? " · sentence by sentence" : " · whole reply at once");
  $("#v-mode-hint").textContent = v.mode === "streaming"
    ? "Stages overlap: transcribe while you talk, speak each sentence as soon as it's written."
    : "Each stage waits for the previous one to finish completely.";
}

// ------------------------------------------------------------------ mic

async function toggle() {
  if (client && client.state !== "off") {
    await client.stop();
    client = null;
    return;
  }
  client = new VoiceClient({
    onState: paintState,
    onLevel: (x) => ($("#v-level").style.width = `${Math.round(x * 100)}%`),
    onPartial: (text) => ($("#v-partial").innerHTML = text ? `<span class="asr-interim">${escapeHtml(text)}</span>` : ""),
    onEvent,
    onTurnDone: paintTurn,
    onError: (msg) => ($("#v-turn").innerHTML = `<div class="note err">${escapeHtml(msg)}</div>`),
  });
  const c = client;
  try {
    $("#v-turn").innerHTML = "";
    await c.start(startMsg(), vs());
  } catch (err) {
    if (c.state !== "off") $("#v-turn").innerHTML = `<div class="note err">${escapeHtml(err.message || String(err))}</div>`;
    await c.stop();
    if (client === c) client = null;
  }
}

function paintState(s) {
  const [dot, label] = STATES[s] || STATES.off;
  $("#v-status").innerHTML = `<span class="${dot}"></span><span>${label}</span>`;
  $("#v-mic").classList.toggle("live", s !== "off");
  $("#v-mic").setAttribute("aria-label", s === "off" ? "Start talking" : "Stop talking");
}

function onEvent(ev) {
  if (ev.type === "asr_final") {
    $("#v-partial").innerHTML = `<span class="asr-final">${escapeHtml(ev.text)}</span>`;
    userBubble(ev.text);
    view = agentTurn();
  } else if (ev.type === "notice") {
    $("#v-turn").innerHTML = `<div class="note">${escapeHtml(ev.message)}</div>`;
  } else if (ev.type === "barge_in") {
    cutView = view;
    view?.finish();
    view = null;
    $("#v-turn").innerHTML = `<div class="note">You interrupted: audio stopped ${Math.round(ev.stop_ms)} ms after you started talking over it.</div>`;
  } else if (ev.type === "interrupted") {
    cutView?.interrupted(ev);
    cutView = null;
  } else if (ev.type === "asr_empty") {
    $("#v-partial").innerHTML = `<span class="asr-interim">Didn't catch that. Try again.</span>`;
  } else if (["delta", "tool", "rag", "done"].includes(ev.type)) {
    view?.onEvent(ev);
  } else if (ev.type === "error") {
    (view ? view.onEvent(ev) : $("#v-turn").replaceChildren(el("div", { class: "note err" }, ev.message)));
  } else if (ev.type === "cart_changed") {
    window.dispatchEvent(new Event("nimbus:cart-changed"));
  }
}

// ------------------------------------------------------------------ latency

const fmt = (ms) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);

function paintTurn(t) {
  view?.finish();
  window.dispatchEvent(new CustomEvent("nimbus:turn-voice", { detail: t }));
  const s = t.timings_ms, c = t.client;
  const rows = [
    ["End of speech (silence wait)", s.endpoint],
    ["Speech recognition", s.asr],
    ["Retrieval (RAG)", s.rag || null],
    ["LLM first token", s.llm_ttft],
    ["Tools", s.tools || null],
    ["TTS first audio", s.tts_ttfb],
    ["Network", c.network],
    ["Playback buffer", c.buffer],
  ];
  view?.note(el("div", { class: "latency-row" },
    `voice · ${t.mode} · you stopped → heard reply ${fmt(c.total)} · ASR ${fmt(s.asr)} · TTS first audio ${fmt(s.tts_ttfb)}`));
  $("#v-turn").innerHTML = `
    <div class="turn-lat">
      <div class="turn-lat-h"><strong>${fmt(c.total)}</strong> from when you stopped talking to the first audio
        <span class="muted">· ${escapeHtml(t.mode)} · ${escapeHtml(t.providers.asr)} → ${escapeHtml(t.providers.llm)} → ${escapeHtml(t.providers.tts)}</span></div>
      <table class="lat-table"><tbody>${rows
        .filter(([, v]) => v != null)
        .map(([k, v]) => `<tr><td>${k}</td><td>${fmt(v)}</td></tr>`)
        .join("")}</tbody></table>
      <p class="field-hint">Stages overlap in streaming mode, so they don't sum to the total. Audio played: ${fmt(t.audio_ms)} ·
        underruns: ${c.underruns}${c.underruns ? " (audible gaps: try a bigger buffer)" : ""}. See the Latency panel for the full breakdown.</p>
    </div>`;
}
