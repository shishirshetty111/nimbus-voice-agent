// Playground: the LLM reasoning layer. Settings (provider, model, knowledge mode, length, memory,
// system prompt) and a text chat with the agent showing tool calls, retrieval, latency and memory.

import { el, escapeHtml } from "../app.js";
import { api, apiStream } from "./api.js";
import { KEY_IN_CHAT, loadKeys, looksLikeKey } from "./keys.js";
import { getSettings, setSettings } from "./settings.js";

const $ = (sel, root = document) => root.querySelector(sel);
const SESSION_KEY = "nimbus_chat_session";
const cfg = { providers: [], defaults: {}, defaultPersona: "", serverKeys: {} };

// ------------------------------------------------------------------ session id

function newSessionId() {
  return "s_" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(36).slice(2) + Date.now().toString(36));
}
function sessionId() {
  try {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) localStorage.setItem(SESSION_KEY, (id = newSessionId()));
    return id;
  } catch {
    return (sessionId.mem ||= newSessionId());
  }
}
function resetSessionId() {
  try {
    localStorage.setItem(SESSION_KEY, newSessionId());
  } catch {
    sessionId.mem = newSessionId();
  }
}

// ------------------------------------------------------------------ settings

function current() {
  const s = getSettings(), d = cfg.defaults;
  const provider = s.provider && cfg.providers.some((p) => p.id === s.provider) ? s.provider : d.provider;
  const prov = cfg.providers.find((p) => p.id === provider);
  const model = s.model && prov.models.some((m) => m.id === s.model) ? s.model : prov.default;
  return {
    provider,
    model,
    persona: s.persona || "",
    length: s.length || d.length,
    verbatim_turns: s.verbatim_turns ?? d.verbatim_turns,
    knowledge_mode: s.knowledge_mode || d.knowledge_mode,
    rag_k: s.rag_k ?? d.rag_k,
    rerank: s.rerank ?? d.rerank,
    tools_enabled: s.tools_enabled,
    latency_mode: s.latency_mode === "fast" ? "fast" : "normal",
  };
}

// ------------------------------------------------------------------ panels

export function chatPanels() {
  return [
    el("div", { class: "panel" }, [
      el("div", { class: "panel-h" }, ["Agent brain ", el("span", { class: "panel-sub" }, "LLM, knowledge, reply length and memory")]),
      el("div", { class: "brain-grid" }, [
        field("Provider", el("select", { class: "inp", id: "c-provider" })),
        field("Model", el("select", { class: "inp", id: "c-model" }), el("span", { class: "field-hint", id: "c-keyhint" })),
        field("Knowledge", seg("c-kmode", [["rag", "RAG (FAISS)"], ["ragless", "RAG-less (full context)"]])),
        field("Reply length", seg("c-length", [["low", "Low"], ["medium", "Medium"], ["high", "High"]])),
        field("Memory: turns kept word for word", range("c-verbatim", 0, 20), el("span", { class: "field-hint" }, "Older turns are folded into a running summary.")),
        field("RAG: chunks per search", range("c-ragk", 1, 10), el("label", { class: "check" }, [el("input", { type: "checkbox", id: "c-rerank" }), " Rerank (+140-400 ms)"])),
      ]),
      el("div", { class: "field", style: "margin-top:1rem" }, [
        el("span", { class: "field-label" }, ["System prompt ", el("button", { class: "linkish", type: "button", id: "c-persona-reset" }, "reset to default")]),
        el("textarea", { class: "inp mono", id: "c-persona", rows: "5", spellcheck: "false" }),
        el("span", { class: "field-hint" }, "Who the agent is and how it should behave. Saved in this browser."),
      ]),
      el("details", { class: "rules" }, [
        el("summary", {}, "Rules always added after your prompt (follow the settings above)"),
        el("pre", { class: "json rules-pre", id: "c-rules" }, "…"),
      ]),
    ]),
    el("div", { class: "panel" }, [
      el("div", { class: "panel-h chat-h" }, [
        el("span", {}, ["Talk to the agent ", el("span", { class: "panel-sub", id: "c-mem" }, "")]),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", id: "c-new" }, "New conversation"),
      ]),
      el("div", { class: "talk-log chat-log", id: "c-log" }, [
        el("p", { class: "muted chat-empty" }, "Ask about a product, a policy, or say “add Nimbus CRM Professional for 3 users to my cart”."),
      ]),
      el("form", { class: "try-row chat-form", id: "c-form" }, [
        el("input", { class: "inp", id: "c-input", placeholder: "Type a message…", autocomplete: "off", maxlength: "2000" }),
        el("button", { class: "btn btn-primary", type: "submit", id: "c-send" }, "Send"),
      ]),
      el("details", { class: "rules" }, [el("summary", {}, "What the agent remembers"), el("div", { id: "c-memview", class: "memview" })]),
    ]),
  ];
}

const field = (label, ...children) => el("div", { class: "field" }, [el("span", { class: "field-label" }, label), ...children]);
const seg = (name, options) =>
  el("div", { class: "seg", role: "radiogroup" }, options.map(([v, l]) => el("label", {}, [el("input", { type: "radio", name, value: v }), el("span", {}, l)])));
const range = (id, min, max) =>
  el("div", { class: "range-wrap" }, [el("input", { type: "range", class: "inp-range", id, min, max }), el("span", { class: "range-val", id: `${id}-val` })]);

// ------------------------------------------------------------------ wiring

export async function setupChat(serverKeys) {
  cfg.serverKeys = serverKeys || {};
  const c = await api("/api/chat/config");
  cfg.providers = c.providers;
  cfg.defaults = { ...c.defaults, provider: c.default_provider };
  cfg.defaultPersona = c.default_persona;

  const s = current();
  $("#c-provider").innerHTML = cfg.providers.map((p) => `<option value="${p.id}">${escapeHtml(p.label)}</option>`).join("");
  $("#c-provider").value = s.provider;
  paintModels();
  document.querySelector(`input[name="c-kmode"][value="${s.knowledge_mode}"]`).checked = true;
  document.querySelector(`input[name="c-length"][value="${s.length}"]`).checked = true;
  $("#c-verbatim").value = s.verbatim_turns;
  $("#c-ragk").value = s.rag_k;
  $("#c-rerank").checked = s.rerank;
  $("#c-persona").value = s.persona || cfg.defaultPersona;
  syncLabels();

  $("#c-provider").addEventListener("change", () => {
    setSettings({ provider: $("#c-provider").value, model: null });
    paintModels();
  });
  $("#c-model").addEventListener("change", () => setSettings({ model: $("#c-model").value }));
  document.querySelectorAll('input[name="c-kmode"]').forEach((r) => r.addEventListener("change", () => (setSettings({ knowledge_mode: r.value }), syncLabels(), refreshRules())));
  document.querySelectorAll('input[name="c-length"]').forEach((r) => r.addEventListener("change", () => (setSettings({ length: r.value }), refreshRules())));
  $("#c-verbatim").addEventListener("input", () => (setSettings({ verbatim_turns: +$("#c-verbatim").value }), syncLabels()));
  $("#c-ragk").addEventListener("input", () => (setSettings({ rag_k: +$("#c-ragk").value }), syncLabels()));
  $("#c-rerank").addEventListener("change", () => setSettings({ rerank: $("#c-rerank").checked }));
  $("#c-persona").addEventListener("input", () => {
    const v = $("#c-persona").value.trim();
    setSettings({ persona: v === cfg.defaultPersona.trim() ? "" : $("#c-persona").value });
  });
  $("#c-persona-reset").addEventListener("click", () => {
    $("#c-persona").value = cfg.defaultPersona;
    setSettings({ persona: "" });
  });
  $("#c-form").addEventListener("submit", (e) => {
    e.preventDefault();
    send();
  });
  $("#c-new").addEventListener("click", newConversation);
  window.addEventListener("nimbus:tools-changed", refreshRules);
  refreshRules();
  loadMemory();
}

function paintModels() {
  const s = current();
  const prov = cfg.providers.find((p) => p.id === s.provider);
  $("#c-model").innerHTML = prov.models.map((m) => `<option value="${m.id}">${escapeHtml(m.label)}</option>`).join("");
  $("#c-model").value = s.model;
  const browserKey = !!loadKeys()[s.provider];
  const serverKey = !!cfg.serverKeys[s.provider];
  $("#c-keyhint").innerHTML = browserKey
    ? `${escapeHtml(prov.label)} key saved in this browser`
    : serverKey
    ? `Using the server's ${escapeHtml(prov.label)} key`
    : `<span class="warn">No ${escapeHtml(prov.label)} key yet: add one in the API keys panel below</span>`;
}

function syncLabels() {
  $("#c-verbatim-val").textContent = $("#c-verbatim").value;
  $("#c-ragk-val").textContent = $("#c-ragk").value;
  const rag = current().knowledge_mode === "rag";
  $("#c-ragk").disabled = !rag;
  $("#c-rerank").disabled = !rag;
}

async function refreshRules() {
  const s = current();
  try {
    const r = await api("/api/chat/rules", { method: "POST", body: { length: s.length, knowledge_mode: s.knowledge_mode, tools_enabled: s.tools_enabled, latency_mode: s.latency_mode } });
    $("#c-rules").textContent = r.rules;
  } catch (err) {
    $("#c-rules").textContent = err.message;
  }
}

// ------------------------------------------------------------------ chat

function bubble(role, text = "") {
  $(".chat-empty")?.remove();
  const b = el("div", { class: "bubble" }, text);
  const row = el("div", { class: `chat ${role}` }, [el("div", { class: "chat-col" }, [b])]);
  $("#c-log").append(row);
  $("#c-log").scrollTop = $("#c-log").scrollHeight;
  return row;
}

// Renders one agent reply as its events arrive. Shared by typed chat and voice turns.
export function agentTurn() {
  const row = bubble("agent");
  const col = row.querySelector(".chat-col");
  const textEl = row.querySelector(".bubble");
  textEl.classList.add("typing");
  const trace = el("div", { class: "trace" });
  col.append(trace);
  const scroll = () => ($("#c-log").scrollTop = $("#c-log").scrollHeight);
  return {
    col,
    onEvent(ev) {
      if (ev.type === "delta") {
        textEl.classList.remove("typing");
        textEl.textContent += ev.text;
      } else if (ev.type === "tool") {
        trace.append(toolChip(ev));
        if (ev.changes_cart) window.dispatchEvent(new Event("nimbus:cart-changed"));
      } else if (ev.type === "rag") {
        trace.append(ragChip(ev));
      } else if (ev.type === "error") {
        col.append(el("div", { class: "note err chat-err" }, ev.message));
      } else if (ev.type === "done") {
        col.append(latencyRow(ev));
        paintMemory(ev.memory);
      }
      scroll();
    },
    note(node) {
      col.append(node);
      scroll();
    },
    finish() {
      textEl.classList.remove("typing");
      if (!textEl.textContent) textEl.remove();
    },
    // Barge-in: show what was heard vs. the unheard rest, and what memory now holds.
    interrupted({ spoken, reply, played_ms }) {
      textEl.classList.remove("typing");
      if (!textEl.isConnected) col.prepend(textEl);
      const rest = reply.startsWith(spoken) ? reply.slice(spoken.length) : reply;
      textEl.replaceChildren(el("span", {}, spoken), el("span", { class: "unheard" }, rest));
      col.append(el("div", { class: "note interrupted-note" },
        `Interrupted after ${(played_ms / 1000).toFixed(1)} s of audio. Memory keeps: "${spoken || "(nothing)"} [cancelled by the user]"`));
      scroll();
    },
  };
}

export const userBubble = (text) => bubble("user", text);
export const chatSettings = () => current();
export const chatSessionId = () => sessionId();

async function send() {
  const input = $("#c-input");
  const message = input.value.trim();
  if (!message) return;
  if (looksLikeKey(message)) {
    input.value = "";
    agentTurn().note(el("div", { class: "note err chat-err" }, KEY_IN_CHAT));
    return;
  }
  input.value = "";
  const btn = $("#c-send");
  btn.disabled = true;
  paintModels(); // re-check the key hint (the user may have just added one)

  bubble("user", message);
  const view = agentTurn();
  try {
    await apiStream("/api/chat", { session_id: sessionId(), message, settings: current() }, (ev) => {
      view.onEvent(ev);
      if (ev.type === "done") window.dispatchEvent(new CustomEvent("nimbus:turn-text", { detail: ev }));
    });
  } catch (err) {
    view.note(el("div", { class: "note err chat-err" }, err.message));
  } finally {
    view.finish();
    btn.disabled = false;
    input.focus();
  }
}

function toolChip(ev) {
  const args = Object.entries(ev.args || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
  return el("details", { class: `tool-call ${ev.ok ? "ok" : "bad"}` }, [
    el("summary", {}, `${ev.ok ? "✓" : "✗"} ${ev.name}(${args}) · ${ev.ms.toFixed(1)} ms${ev.parallel ? " · ran in parallel" : ""}`),
    el("pre", { class: "json" }, JSON.stringify(ev.result, null, 2)),
  ]);
}

function ragChip(ev) {
  return el("details", { class: "tool-call ok" }, [
    el("summary", {}, `Retrieved ${ev.hits.length} chunks · ${ev.timings_ms.total.toFixed(0)} ms`),
    el("ul", { class: "rag-list" }, ev.hits.map((h) => el("li", {}, h.title))),
  ]);
}

function latencyRow(ev) {
  const t = ev.timings_ms, u = ev.usage;
  const parts = [
    `first word ${fmt(t.first_word)}`,
    `total ${fmt(t.total)}`,
    `LLM ${fmt(t.llm)} in ${ev.rounds} round${ev.rounds === 1 ? "" : "s"}`,
    ...(t.rag ? [`RAG ${fmt(t.rag)}`] : []),
    ...(ev.tool_calls ? [`tools ${fmt(t.tools)} (${ev.tool_calls})`] : []),
    `${(u.input_tokens || 0).toLocaleString()} in${u.cached_tokens ? ` (${u.cached_tokens.toLocaleString()} cached)` : ""} / ${(u.output_tokens || 0).toLocaleString()} out`,
  ];
  return el("div", { class: "latency-row", title: `${ev.provider} · ${ev.model}` }, parts.join(" · "));
}

const fmt = (ms) => (ms == null ? "–" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);

// ------------------------------------------------------------------ memory

async function loadMemory() {
  try {
    const m = await api(`/api/chat/${sessionId()}?verbatim=${current().verbatim_turns}`);
    paintMemory(m);
    m.transcript.forEach((t) => {
      bubble("user", t.user);
      bubble("agent", t.assistant.split("\n[Actions taken")[0]);
    });
  } catch {}
}

function paintMemory(m) {
  $("#c-mem").textContent = m.turns
    ? `${m.turns} turn${m.turns === 1 ? "" : "s"} · ${m.verbatim} word for word · ${m.summarized} summarized${m.summarizing ? " (summarizing…)" : ""}`
    : "";
  $("#c-memview").innerHTML = m.turns
    ? `<p class="field-hint"><strong>Summary of older turns:</strong> ${escapeHtml(m.summary || "(nothing summarized yet)")}</p>
       <ol class="memlist">${m.transcript
         .map((t) => `<li class="${t.in_window ? "kept" : "dropped"}"><span>${t.in_window ? "word for word" : t.summarized ? "in summary" : "pending summary"}</span> ${escapeHtml(t.user)}</li>`)
         .join("")}</ol>`
    : `<p class="field-hint">Nothing yet.</p>`;
}

async function newConversation() {
  try {
    await api(`/api/chat/${sessionId()}/reset`, { method: "POST" });
  } catch {}
  resetSessionId();
  $("#c-log").innerHTML = "";
  $("#c-log").append(el("p", { class: "muted chat-empty" }, "New conversation. The cart is kept."));
  paintMemory({ turns: 0 });
}

