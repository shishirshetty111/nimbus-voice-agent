// Latency dashboard: per-stage breakdown of every turn, which stage costs the most, history, and
// streaming-vs-batch / RAG-vs-RAG-less comparisons, plus an A/B runner that sends one question
// through both variants back to back.
//
// Turns arrive as "nimbus:turn-voice" / "nimbus:turn-text" events (and from the A/B runner) and are
// kept in this browser's localStorage. Stages are split along the critical path, so they add up to
// the total; whatever the stages don't explain is shown as "other".

import { el, escapeHtml } from "../app.js";
import { apiBase } from "./api.js";
import { chatSettings } from "./chat-panel.js";
import { loadKeys } from "./keys.js";
import { getSettings } from "./settings.js";
import { DARK_TEXT, MAX_TURNS, STAGES, finish, fmt, fromTextTurn, fromVoiceTurn, loadTurns, pos, saveTurns } from "./latency-model.js";

const $ = (sel, root = document) => root.querySelector(sel);
let turns = loadTurns();
const save = () => saveTurns(turns);

export function record(rec) {
  if (!rec) return;
  turns.push(rec);
  turns = turns.slice(-MAX_TURNS);
  save();
  render();
}
// Panels publish raw results; converting them here keeps the panels free of dashboard imports.
window.addEventListener("nimbus:turn-voice", (e) => record(fromVoiceTurn(e.detail)));
window.addEventListener("nimbus:turn-text", (e) => record(fromTextTurn(e.detail)));

// ------------------------------------------------------------------ panel

export function dashboardPanel() {
  return el("div", { class: "panel", id: "dash" }, [
    el("div", { class: "panel-h dash-h" }, [
      el("span", {}, ["Latency ", el("span", { class: "panel-sub", id: "d-sub" }, "")]),
      el("span", { class: "tools-bulk" }, [
        el("button", { class: "btn btn-ghost btn-sm", type: "button", id: "d-csv" }, "Export CSV"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", id: "d-clear" }, "Clear"),
      ]),
    ]),
    el("div", { id: "d-body" }),
    el("div", { class: "ab" }, [
      el("div", { class: "panel-h", style: "margin-top:1.4rem" }, ["A/B runner ",
        el("span", { class: "panel-sub" }, "same question, both variants, back to back (text in, voice out, no mic)")]),
      el("div", { class: "try-row" }, [
        el("input", { class: "inp", id: "ab-q", value: "What does Nimbus CRM Professional cost, and is there a free trial?", maxlength: "500" }),
        el("select", { class: "inp inp-sm", id: "ab-rounds", title: "Rounds", html: "<option value='1'>×1</option><option value='2' selected>×2</option><option value='3'>×3</option>" }),
      ]),
      el("div", { class: "try-row" }, [
        el("button", { class: "btn btn-primary btn-sm", type: "button", id: "ab-mode" }, "Streaming vs batch"),
        el("button", { class: "btn btn-primary btn-sm", type: "button", id: "ab-know" }, "RAG vs RAG-less"),
        el("button", { class: "btn btn-primary btn-sm", type: "button", id: "ab-fast" }, "Normal vs ⚡ Low-latency"),
        el("span", { class: "field-hint", id: "ab-status" }, "Uses the agent brain and voice settings above, a throwaway conversation and cart."),
      ]),
    ]),
    el("div", { class: "dash-tip", id: "d-tip", hidden: "" }),
  ]);
}

export function setupDashboard() {
  $("#d-clear").addEventListener("click", () => {
    turns = [];
    save();
    render();
  });
  $("#d-csv").addEventListener("click", exportCsv);
  $("#ab-mode").addEventListener("click", () => runAB([{ mode: "streaming" }, { mode: "batch" }]));
  $("#ab-know").addEventListener("click", () => runAB([{ knowledge: "rag" }, { knowledge: "ragless" }]));
  $("#ab-fast").addEventListener("click", () => runAB([{ latency: "normal" }, { latency: "fast" }]));
  $("#d-body").addEventListener("mousemove", tip);
  $("#d-body").addEventListener("mouseleave", () => ($("#d-tip").hidden = true));
  render();
}

// ------------------------------------------------------------------ render

function render() {
  const body = $("#d-body");
  if (!body) return;
  $("#d-sub").textContent = turns.length ? `${turns.length} turn${turns.length === 1 ? "" : "s"} recorded in this browser` : "";
  if (!turns.length) {
    body.innerHTML = `<p class="field-hint">Talk to the agent (or type, or run the A/B runner below) and every turn's latency lands here.</p>`;
    return;
  }
  const last = turns[turns.length - 1];
  const ranked = STAGES.map((s) => ({ ...s, ms: pos(last.stages[s.key]) })).filter((s) => s.ms >= 0.5).sort((a, b) => b.ms - a.ms);
  const top = ranked[0];
  body.innerHTML = "";
  body.append(
    el("div", { class: "stat-tiles dash-tiles" }, [
      tile("Last turn", fmt(last.total), last.kind === "text" ? "message sent → first word" : "you stopped → first audio", true),
      tile("Biggest share", top ? top.label : "–", top ? `${Math.round((top.ms / last.total) * 100)}% · ${fmt(top.ms)}` : ""),
      tile("Streaming avg", fmt(mean(group("mode", "streaming"))), `${group("mode", "streaming").length} turns`),
      tile("Batch avg", fmt(mean(group("mode", "batch"))), `${group("mode", "batch").length} turns`),
    ]),
    el("h4", { class: "dash-h4" }, `Where the time went · last turn (${escapeHtml(last.kind)}${last.mode && last.mode !== "text" ? ` · ${last.mode}` : ""})`),
    el("div", { html: shareBar(last) }),
    rankTable(ranked, last.total),
    el("h4", { class: "dash-h4" }, "Every turn (newest first)"),
    el("div", { html: historyBars(turns.slice(-15).reverse()) }),
    legend(),
    el("h4", { class: "dash-h4" }, "Streaming vs batch"),
    compare("mode", [["streaming", "Streaming"], ["batch", "Batch"]], (t) => t.kind !== "text", "total"),
    el("h4", { class: "dash-h4" }, "Normal vs ⚡ Low-latency"),
    compare("profile", [["normal", "Normal"], ["fast", "⚡ Low-latency"]], (t) => t.kind !== "text", "total"),
    el("h4", { class: "dash-h4" }, "RAG vs RAG-less"),
    compare("knowledge", [["rag", "RAG (FAISS)"], ["ragless", "RAG-less (full context)"]], (t) => t.mode === "streaming", "think"),
    tableView()
  );
}

const tile = (label, value, sub, hero) =>
  el("div", { class: `stat-tile${hero ? " hero" : ""}` }, [el("span", { class: "stat-label" }, label), el("strong", {}, value), el("span", { class: "stat-sub" }, sub)]);

const group = (field, value, pred = (t) => t.kind !== "text") => turns.filter((t) => t[field] === value && pred(t)).map((t) => t.total);
const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
const pct = (xs, p) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))];
};

// One horizontal bar split into stages, scaled to `max`. 2px gaps between segments; labels when they fit.
function stackSvg(stages, max, { width = 1000, height = 30, labels = true, row = "" } = {}) {
  const items = STAGES.map((s) => ({ ...s, ms: pos(stages[s.key]) })).filter((s) => s.ms >= 0.5);
  let x = 0, out = "";
  const gap = 2;
  for (const s of items) {
    const w = (s.ms / max) * width;
    const drawn = Math.max(1, w - gap);
    out += `<rect x="${x.toFixed(1)}" y="0" width="${drawn.toFixed(1)}" height="${height}" rx="3" fill="${s.color}"
      data-tip="${escapeHtml(`${row}${s.label}: ${fmt(s.ms)}`)}"/>`;
    if (labels && drawn > 70) {
      out += `<text x="${(x + drawn / 2).toFixed(1)}" y="${height / 2 + 5}" text-anchor="middle" class="seg-label${DARK_TEXT.has(s.color) ? " dark" : ""}">${escapeHtml(fmt(s.ms))}</text>`;
    }
    x += w;
  }
  return out;
}

function shareBar(t) {
  return `<svg class="dash-svg" viewBox="0 0 1000 34" preserveAspectRatio="none" role="img"
    aria-label="Share of the last turn's latency by stage">${stackSvg(t.stages, t.total, { height: 34 })}</svg>`;
}

function rankTable(ranked, total) {
  return el("table", { class: "rank" }, [
    el("tbody", {}, ranked.map((s, i) =>
      el("tr", { class: i === 0 ? "top" : "" }, [
        el("td", {}, [el("i", { style: `background:${s.color}` }), s.label]),
        el("td", { class: "num" }, fmt(s.ms)),
        el("td", { class: "num" }, `${Math.round((s.ms / total) * 100)}%`),
        el("td", { class: "meter-cell", html: `<span class="meter"><b style="width:${((s.ms / ranked[0].ms) * 100).toFixed(1)}%;background:${s.color}"></b></span>` }),
        el("td", { class: "hint" }, s.hint),
      ])
    )),
  ]);
}

function historyBars(list) {
  const max = Math.max(...list.map((t) => t.total));
  const rowH = 22, gap = 8, labelW = 230, barW = 640;
  const rows = list.map((t, i) => {
    const y = i * (rowH + gap);
    const name = `${t.profile === "fast" ? "⚡ " : ""}${t.kind === "ab" ? "A/B " : ""}${t.kind === "text" ? "text" : t.mode}${t.knowledge ? ` · ${t.knowledge === "ragless" ? "RAG-less" : "RAG"}` : ""}`;
    return `<g transform="translate(0,${y})">
      <text x="0" y="${rowH / 2 + 5}" class="row-label">${escapeHtml(name)}</text>
      <g transform="translate(${labelW},0)">${stackSvg(t.stages, max, { width: barW, height: rowH, labels: false, row: `${name} · ` })}</g>
      <text x="${labelW + (t.total / max) * barW + 8}" y="${rowH / 2 + 5}" class="row-total">${escapeHtml(fmt(t.total))}</text></g>`;
  });
  const h = list.length * (rowH + gap);
  return `<svg class="dash-svg hist" viewBox="0 0 ${labelW + barW + 90} ${h}" role="img" aria-label="Latency of recent turns by stage">${rows.join("")}</svg>`;
}

function legend() {
  return el("div", { class: "viz-legend" }, STAGES.map((s) =>
    el("span", { class: "viz-legend-item", title: s.hint }, [el("i", { style: `background:${s.color}` }), s.label])));
}

// Mean stage breakdown per variant. metric "total" = whole turn; "think" = retrieval + tools + LLM only,
// which is comparable across voice, text and A/B turns (knowledge mode only changes this part).
function compare(field, variants, pred, metric) {
  const keys = metric === "think" ? ["rag", "tools", "llm"] : STAGES.map((s) => s.key);
  const rows = variants.map(([value, label]) => {
    const list = turns.filter((t) => t[field] === value && pred(t));
    const vals = list.map((t) => (metric === "think" ? keys.reduce((a, k) => a + pos(t.stages[k]), 0) : t.total));
    const stages = Object.fromEntries(keys.map((k) => [k, mean(list.map((t) => pos(t.stages[k]))) || 0]));
    const tokens = mean(list.map((t) => t.tokens?.input_tokens).filter((x) => x != null));
    return { label, n: list.length, mean: mean(vals), p50: pct(vals, 50), p95: pct(vals, 95), stages, tokens };
  });
  const have = rows.filter((r) => r.n);
  if (!have.length) return el("p", { class: "field-hint" }, "No turns for either variant yet. Try the A/B runner below.");
  const max = Math.max(...have.map((r) => r.mean));
  const labelW = 230, barW = 640, rowH = 26;
  const svg = rows.map((r, i) => {
    const y = i * (rowH + 10);
    const meanTotal = keys.reduce((a, k) => a + r.stages[k], 0);
    return `<g transform="translate(0,${y})"><text x="0" y="${rowH / 2 + 5}" class="row-label">${escapeHtml(r.label)}</text>
      ${r.n ? `<g transform="translate(${labelW},0)">${stackSvg(r.stages, max, { width: barW, height: rowH, labels: false, row: `${r.label} mean · ` })}</g>
      <text x="${labelW + (meanTotal / max) * barW + 8}" y="${rowH / 2 + 5}" class="row-total">${escapeHtml(fmt(r.mean))}</text>`
        : `<text x="${labelW}" y="${rowH / 2 + 5}" class="row-label muted">no turns yet</text>`}</g>`;
  }).join("");
  const a = rows[0], b = rows[1];
  const diff = a.n && b.n ? Math.abs(a.mean - b.mean) : null;
  const verdict = diff == null ? ""
    : diff < 20 ? `${a.label} and ${b.label} are about the same (within ${fmt(diff)}).`
    : `${a.mean <= b.mean ? a.label : b.label} is ${fmt(diff)} faster on average${metric === "think" ? " to the first sentence" : ""}.`;
  return el("div", {}, [
    el("div", { html: `<svg class="dash-svg hist" viewBox="0 0 ${labelW + barW + 90} ${rows.length * (rowH + 10)}" role="img">${svg}</svg>` }),
    el("table", { class: "cmp" }, [
      el("thead", { html: `<tr><th></th><th>turns</th><th>mean</th><th>p50</th><th>p95</th>${metric === "think" ? "<th>input tokens</th>" : ""}</tr>` }),
      el("tbody", { html: rows.map((r) => `<tr><td>${escapeHtml(r.label)}</td><td>${r.n}</td><td>${fmt(r.mean)}</td><td>${fmt(r.p50)}</td><td>${fmt(r.p95)}</td>${
        metric === "think" ? `<td>${r.tokens == null ? "–" : Math.round(r.tokens).toLocaleString()}</td>` : ""}</tr>`).join("") }),
    ]),
    verdict ? el("p", { class: "verdict" }, verdict) : null,
    metric === "think"
      ? el("p", { class: "field-hint" }, "Compares retrieval + tools + LLM time to the first sentence (streaming voice and A/B turns only, so the pipeline and metric are the same), the part the knowledge mode changes. RAG-less sends the whole knowledge base; repeat runs are usually faster once the provider caches that prompt.")
      : el("p", { class: "field-hint" }, "Whole turn: you stopped talking (or the A/B question was sent) → first audio."),
  ]);
}

function tableView() {
  const head = ["when", "kind", "profile", "mode", "knowledge", "total", "filler", ...STAGES.map((s) => s.label)];
  return el("details", { class: "rules" }, [
    el("summary", {}, "Table view (every recorded turn)"),
    el("div", { class: "table-scroll" }, [
      el("table", { class: "cmp" }, [
        el("thead", { html: `<tr>${head.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr>` }),
        el("tbody", { html: [...turns].reverse().map((t) => `<tr><td>${new Date(t.at).toLocaleTimeString()}</td><td>${t.kind}</td><td>${t.profile || "normal"}</td><td>${t.mode || ""}</td><td>${t.knowledge || ""}</td><td>${fmt(t.total)}</td><td>${t.filler ? fmt(t.filler) : ""}</td>${
          STAGES.map((s) => `<td>${t.stages[s.key] ? fmt(t.stages[s.key]) : ""}</td>`).join("")}</tr>`).join("") }),
      ]),
    ]),
  ]);
}

function tip(e) {
  const t = e.target.closest("[data-tip]");
  const box = $("#d-tip");
  if (!t) return (box.hidden = true);
  box.textContent = t.dataset.tip;
  box.hidden = false;
  const r = $("#dash").getBoundingClientRect();
  box.style.left = `${e.clientX - r.left + 12}px`;
  box.style.top = `${e.clientY - r.top + 12}px`;
}

function exportCsv() {
  const head = ["timestamp", "kind", "profile", "mode", "knowledge", "total_ms", "filler_ms", ...STAGES.map((s) => `${s.key}_ms`), "input_tokens", "underruns"];
  const rows = turns.map((t) => [new Date(t.at).toISOString(), t.kind, t.profile || "normal", t.mode || "", t.knowledge || "",
    Math.round(t.total), t.filler ? Math.round(t.filler) : "",
    ...STAGES.map((s) => Math.round(pos(t.stages[s.key]))), t.tokens?.input_tokens ?? "", t.underruns ?? ""]);
  const csv = [head, ...rows].map((r) => r.join(",")).join("\n");
  const a = el("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "nimbus-latency.csv" });
  document.body.append(a);
  a.click();
  a.remove();
}

// ------------------------------------------------------------------ A/B runner

async function runAB(variants) {
  const q = $("#ab-q").value.trim();
  const rounds = +$("#ab-rounds").value;
  const status = $("#ab-status");
  if (!q) return;
  document.querySelectorAll(".ab button").forEach((b) => (b.disabled = true));
  try {
    for (let r = 1; r <= rounds; r++) {
      for (const v of variants) {
        status.textContent = `Round ${r}/${rounds} · ${v.latency ? (v.latency === "fast" ? "Low-latency" : "Normal") : v.mode || (v.knowledge === "ragless" ? "RAG-less" : "RAG")}…`;
        record(await abTurn(q, v));
      }
    }
    status.textContent = "Done. Results are in the comparison charts above.";
  } catch (err) {
    status.textContent = `A/B run failed: ${err.message}`;
  } finally {
    document.querySelectorAll(".ab button").forEach((b) => (b.disabled = false));
  }
}

function abTurn(question, variant) {
  const s = getSettings();
  const rand = () => Math.random().toString(36).slice(2, 12);
  const chat = { ...chatSettings(), ...(variant.knowledge ? { knowledge_mode: variant.knowledge } : {}),
                 ...(variant.latency ? { latency_mode: variant.latency } : {}) };
  const mode = variant.latency === "fast" ? "streaming" : variant.mode || s.voice_mode || "streaming"; // fast = streaming
  return new Promise((resolve, reject) => {
    const ws = new WebSocket((apiBase() || location.origin).replace(/^http/, "ws") + "/ws/voice");
    ws.binaryType = "arraybuffer";
    let sentAt, firstByteAt, error;
    const timer = setTimeout(() => (ws.close(), reject(new Error("timed out after 60 s"))), 60000);
    ws.onopen = () => ws.send(JSON.stringify({
      type: "start", keys: loadKeys(), cart_id: `ab-cart-${rand()}`, session_id: `ab_${rand()}`,  // no memory, no cart side effects
      settings: { mode, asr_provider: "browser", tts_provider: s.tts_provider || "openai", tts_voice: s.tts_voice || null, chat },
    }));
    ws.onmessage = (m) => {
      if (typeof m.data !== "string") return void (firstByteAt ||= performance.now());
      const ev = JSON.parse(m.data);
      if (ev.type === "ready") {
        sentAt = performance.now();
        ws.send(JSON.stringify({ type: "text", text: question, asr_ms: 0, endpoint_ms: 0 }));
      } else if (ev.type === "error") {
        error = ev.message;
      } else if (ev.type === "turn_done") {
        clearTimeout(timer);
        ws.close();
        if (!firstByteAt) return reject(new Error(error || "no audio came back"));
        const t = ev.timings_ms, total = firstByteAt - sentAt;
        const think = pos((t.tts_wait ?? t.server_first_audio) - pos(t.asr));
        const rag = pos(t.rag), tools = pos(t.tools_only);
        resolve(finish({
          at: Date.now(), kind: "ab", mode, knowledge: ev.knowledge || chat.knowledge_mode, profile: ev.profile || "normal",
          providers: ev.providers, total,
          stages: { rag, llm: pos(think - rag - tools), tools, tts: pos(t.tts_ttfb), network: pos(total - pos(t.server_first_audio)) },
          tokens: ev.usage,
        }));
      }
    };
    ws.onerror = () => (clearTimeout(timer), reject(new Error("could not connect to the backend")));
  });
}
