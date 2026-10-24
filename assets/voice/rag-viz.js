// RAG Lab: chunk-vector map (UMAP 2D/3D), query explorer with top-k / candidates / rerank
// controls, ranked hits with rank movement, and per-stage retrieval latency.

import { el, escapeHtml } from "../app.js";
import { api, apiBase, apiStream, setApiBase } from "./api.js";
import { mountKeysPanel } from "./keys.js";

// Categorical palette (dataviz reference instance, light). Fixed order, never cycled.
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const NEUTRAL = "#9a9890"; // "Company & policies" - not a category, so it gets a neutral, not a hue
const STAGE = { embed: SERIES[0], search: SERIES[1], rerank: SERIES[2] };

const TYPE_LABELS = {
  product_overview: "Product overview",
  product_features: "Features & specs",
  tier: "Pricing tier",
  faq: "FAQ",
  addon: "Add-ons",
  category: "Product family",
  policy: "Policy",
  company: "Company",
};

const state = {
  catalog: null,
  info: null,
  points: [],
  dim: 2,
  colorBy: "category",
  embedder: "bge-small",
  last: null, // last query result
};

const $ = (sel, root = document) => root.querySelector(sel);

export async function renderRag(catalog) {
  state.catalog = catalog;
  const app = document.querySelector("#app");
  app.append(
    el("section", { class: "console-hero" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "RAG Lab"),
        el("h1", {}, "See how Nimbus finds answers"),
        el("p", {
          class: "lead",
          html:
            "Every chunk of the Nimbus knowledge base is a point. Similar content clusters together. " +
            "Ask a question to see which chunks FAISS retrieves, how the reranker reorders them, and what each step costs in milliseconds.",
        }),
        el("div", { id: "backend-pill", class: "pill" }, [el("span", { class: "dot" }), el("span", {}, "Connecting to backend…")]),
      ]),
    ]),
    el("section", { class: "section", style: "padding-top:32px" }, [
      el("div", { class: "container page", id: "rag-page" }, [
        el("div", { id: "rag-error" }),
        el("div", { class: "stat-tiles", id: "rag-stats" }),
        queryPanel(),
        answerPanel(),
        el("div", { class: "rag-grid" }, [
          el("div", { class: "panel" }, [
            el("div", { class: "panel-h" }, ["Vector map ", el("span", { class: "panel-sub", id: "map-sub" }, "")]),
            el("div", { id: "rag-plot", class: "rag-plot", role: "img", "aria-label": "Scatter plot of knowledge chunks projected to 2D" }),
            el("p", {
              class: "field-hint",
              html:
                "Projection by UMAP (cosine). Nearby points are similar, but distances on screen are approximate: ranking happens in the full vector space. " +
                "Click a legend entry to hide it; double-click to isolate it.",
            }),
          ]),
          el("div", { class: "panel" }, [
            el("div", { class: "panel-h" }, ["Retrieval ", el("span", { class: "panel-sub", id: "res-sub" }, "run a query")]),
            el("div", { id: "rag-timing" }),
            el("div", { id: "rag-hits", class: "rag-hits" }, [
              el("p", { class: "muted", style: "font-size:.9rem" }, "Results appear here. Try one of the example questions."),
            ]),
          ]),
        ]),
        el("div", { id: "keys-mount" }),
        backendPanel(),
      ]),
    ])
  );

  bindControls();
  await connect();
}

// ------------------------------------------------------------------ panels

function queryPanel() {
  const examples = [
    "how do refunds work",
    "is there a free trial",
    "what does Nimbus Payroll cost",
    "which product handles kanban boards",
    "where can my data be stored",
  ];
  return el("div", { class: "panel" }, [
    el("div", { class: "panel-h" }, ["Query the knowledge base"]),
    el("form", { class: "try-row", id: "rag-form" }, [
      el("input", { class: "inp", id: "q", placeholder: "Ask anything about Nimbus…", autocomplete: "off", maxlength: "500" }),
      el("button", { class: "btn btn-primary", type: "submit", id: "q-go" }, "Retrieve"),
    ]),
    el(
      "div",
      { class: "tag-row", style: "margin-bottom:.4rem" },
      examples.map((q) => el("button", { class: "tag tag-btn", type: "button", "data-q": q }, q))
    ),
    el("div", { class: "rag-controls" }, [
      rangeField("k", "Top-k chunks returned", 1, 20, 5, "Chunks passed to the LLM."),
      rangeField("cand", "Candidates for reranking", 5, 50, 10, "FAISS returns this many; the reranker keeps the best k."),
      el("div", { class: "field" }, [
        el("span", { class: "field-label" }, "Reranking"),
        el("label", { class: "check" }, [el("input", { type: "checkbox", id: "rerank", checked: "" }), " Rerank with a cross-encoder"]),
        el("select", { class: "inp", id: "reranker" }),
      ]),
      el("div", { class: "field" }, [
        el("span", { class: "field-label" }, "Embedding model"),
        el("select", { class: "inp", id: "embedder" }),
        el("span", { class: "field-hint", id: "embedder-hint" }, ""),
      ]),
      el("div", { class: "field" }, [
        el("span", { class: "field-label" }, "View"),
        segmented("dim", [["2", "2D"], ["3", "3D"]], "2"),
      ]),
      el("div", { class: "field" }, [
        el("span", { class: "field-label" }, "Color by"),
        segmented("colorby", [["category", "Product family"], ["type", "Chunk type"]], "category"),
      ]),
    ]),
  ]);
}

function backendPanel() {
  const input = el("input", { class: "inp", id: "api-base", placeholder: location.origin, value: apiBase() });
  return el("details", { class: "settings-panel" }, [
    el("summary", { html: `<span>Backend <span class="panel-sub">${escapeHtml(apiBase() || location.origin)}</span></span>` }),
    el("div", { class: "keys-body" }, [
      el("div", { class: "try-row" }, [
        input,
        el("button", {
          class: "btn btn-ghost btn-sm",
          type: "button",
          onclick: () => {
            setApiBase(input.value.trim());
            location.reload();
          },
        }, "Save and reload"),
      ]),
      el("p", { class: "field-hint" }, "Leave empty when this page is served by the backend itself."),
    ]),
  ]);
}

function rangeField(id, label, min, max, value, hint) {
  return el("div", { class: "field" }, [
    el("span", { class: "field-label" }, label),
    el("div", { class: "range-wrap" }, [
      el("input", { type: "range", class: "inp-range", id, min, max, value, "aria-label": label }),
      el("span", { class: "range-val", id: `${id}-val` }, String(value)),
    ]),
    el("span", { class: "field-hint" }, hint),
  ]);
}

function segmented(name, options, value) {
  return el(
    "div",
    { class: "seg", role: "radiogroup" },
    options.map(([v, label]) =>
      el("label", {}, [
        el("input", { type: "radio", name, value: v, ...(v === value ? { checked: "" } : {}) }),
        el("span", {}, label),
      ])
    )
  );
}

// ------------------------------------------------------------------ wiring

function bindControls() {
  const k = $("#k"), cand = $("#cand");
  const syncRange = () => {
    if (+cand.value < +k.value) cand.value = k.value;
    $("#k-val").textContent = k.value;
    $("#cand-val").textContent = cand.value;
    cand.disabled = !$("#rerank").checked;
    $("#reranker").disabled = !$("#rerank").checked;
  };
  [k, cand, $("#rerank")].forEach((n) => n.addEventListener("input", syncRange));
  syncRange();
  // Re-run the current query when a retrieval knob changes.
  [k, cand, $("#rerank"), $("#reranker")].forEach((n) => n.addEventListener("change", () => state.last && runQuery()));

  $("#rag-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runQuery();
  });
  document.querySelectorAll(".tag-btn").forEach((b) =>
    b.addEventListener("click", () => {
      $("#q").value = b.dataset.q;
      runQuery();
    })
  );
  document.querySelectorAll('input[name="dim"]').forEach((r) =>
    r.addEventListener("change", async () => {
      state.dim = +r.value;
      await loadPoints();
      if (state.last) runQuery();
    })
  );
  document.querySelectorAll('input[name="colorby"]').forEach((r) =>
    r.addEventListener("change", () => {
      state.colorBy = r.value;
      drawPlot();
    })
  );
  $("#embedder").addEventListener("change", async (e) => {
    state.embedder = e.target.value;
    await loadPoints();
    if (state.last) runQuery();
  });
}

async function connect() {
  const pill = $("#backend-pill");
  let serverKeys = null;
  try {
    const [info, keys] = await Promise.all([api("/api/rag/info"), api("/api/keys/status")]);
    state.info = info;
    serverKeys = keys;
    pill.className = "pill";
    pill.lastChild.textContent = `Backend connected · ${apiBase() || location.origin}`;
  } catch (err) {
    pill.className = "pill pill-bad";
    pill.lastChild.textContent = "Backend offline";
    showError(err.message);
  }
  mountKeysPanel($("#keys-mount"), serverKeys);
  if (!state.info) return;
  setupAnswer();

  const d = state.info.defaults;
  $("#reranker").innerHTML = state.info.rerankers
    .map((r) => `<option value="${r.key}" ${r.key === d.reranker ? "selected" : ""}>${escapeHtml(r.model)}</option>`)
    .join("");
  $("#embedder").innerHTML = state.info.embedders
    .map((e) => `<option value="${e.key}" ${e.key === d.embedder ? "selected" : ""}>${escapeHtml(e.label)}</option>`)
    .join("");
  $("#embedder-hint").textContent = "OpenAI needs a key; its index is built on first use (~282 embeddings).";
  await loadPoints();
  // ?q=... runs a query on load, so a specific retrieval can be shared as a link.
  const q = new URLSearchParams(location.search).get("q");
  if (q) {
    $("#q").value = q;
    runQuery();
  }
}

async function loadPoints() {
  showError("");
  $("#map-sub").textContent = "loading…";
  try {
    const res = await api(`/api/rag/points?embedder=${encodeURIComponent(state.embedder)}&dim=${state.dim}`);
    state.points = res.points;
    state.manifest = res.manifest;
    paintStats();
    $("#map-sub").textContent = `${res.points.length} chunks · ${res.manifest.dim}-d → ${state.dim}D`;
    drawPlot();
  } catch (err) {
    $("#map-sub").textContent = "";
    showError(err.message);
  }
}

async function runQuery() {
  const query = $("#q").value.trim();
  if (!query || !state.points.length) return;
  const btn = $("#q-go");
  btn.disabled = true;
  btn.textContent = "Retrieving…";
  showError("");
  try {
    state.last = await api("/api/rag/query", {
      method: "POST",
      body: {
        query,
        k: +$("#k").value,
        candidates: +$("#cand").value,
        rerank: $("#rerank").checked,
        reranker: $("#reranker").value,
        embedder: state.embedder,
        dim: state.dim,
      },
    });
    paintTiming();
    paintHits();
    drawPlot();
    $("#answer-go").disabled = !state.answerReady;
    $("#answer-hint").textContent = `Will answer "${state.last.query}" using the ${state.last.hits.length} chunks above (RAG) and/or the whole knowledge base (RAG-less).`;
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Retrieve";
  }
}

function showError(msg) {
  $("#rag-error").innerHTML = msg ? `<div class="note err">${escapeHtml(msg)}</div>` : "";
}

// ------------------------------------------------------------------ stats

function paintStats() {
  const m = state.manifest, info = state.info;
  const tiles = [
    ["Embedding model", m.model, `${m.dim} dimensions`],
    ["Vector index", "FAISS IndexFlatIP", "exact cosine search"],
    ["Chunks indexed", String(m.count), "products, tiers, FAQs, policies"],
    ["Top-k (default)", String(info.defaults.k), `${info.defaults.candidates} candidates reranked`],
    ["Reranker", (info.rerankers.find((r) => r.key === info.defaults.reranker) || {}).model?.split("/").pop() || "-", "cross-encoder"],
    ["RAG-less context", `${info.context.tokens.toLocaleString()} tokens`, "context.md, whole file in prompt"],
  ];
  $("#rag-stats").innerHTML = tiles
    .map(
      ([label, value, sub]) =>
        `<div class="stat-tile"><span class="stat-label">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><span class="stat-sub">${escapeHtml(sub)}</span></div>`
    )
    .join("");
}

// ------------------------------------------------------------------ timing

function paintTiming() {
  const r = state.last, t = r.timings_ms;
  const stages = [["embed", "Embed query"], ["search", "FAISS search"], ["rerank", "Rerank"]].filter(([s]) => s !== "rerank" || r.rerank);
  const sum = stages.reduce((a, [s]) => a + t[s], 0) || 1;
  const W = 100, gap = 0.6;
  let x = 0;
  const segs = stages.map(([s, label]) => {
    const w = Math.max((t[s] / sum) * W - gap, 0.4);
    const seg = `<rect x="${x}" y="0" width="${w}" height="14" rx="2" fill="${STAGE[s]}"><title>${label}: ${t[s].toFixed(1)} ms</title></rect>`;
    x += w + gap;
    return seg;
  });
  $("#rag-timing").innerHTML = `
    <div class="timing-head"><span class="timing-total">${t.total.toFixed(0)}<small> ms</small></span>
      <span class="muted">total retrieval latency</span></div>
    <svg class="timing-bar" viewBox="0 0 100 14" preserveAspectRatio="none" role="img" aria-label="Retrieval latency by stage">${segs.join("")}</svg>
    <div class="timing-legend">${stages
      .map(([s, label]) => `<span><i style="background:${STAGE[s]}"></i>${label} <strong>${t[s].toFixed(1)} ms</strong> <em>${Math.round((t[s] / sum) * 100)}%</em></span>`)
      .join("")}</div>`;
  $("#res-sub").textContent = `top ${r.k} of ${r.rerank ? r.candidates + " candidates" : "FAISS results"} · ${r.projection}`;
}

// ------------------------------------------------------------------ hits

function paintHits() {
  const r = state.last;
  const box = $("#rag-hits");
  box.innerHTML = "";
  r.hits.forEach((h) => {
    const moved = r.rerank ? h.rank_vec - h.rank : 0;
    const move = !r.rerank ? "" : moved > 0
      ? `<span class="move up" title="Reranker moved it up from #${h.rank_vec}">▲ ${moved}</span>`
      : moved < 0
      ? `<span class="move down" title="Reranker moved it down from #${h.rank_vec}">▼ ${-moved}</span>`
      : `<span class="move same" title="Same rank as vector search">·</span>`;
    const body = escapeHtml(h.text.slice(h.title.length + 1));
    box.append(
      el("details", { class: "hit" }, [
        el("summary", {
          html: `<span class="hit-rank">${h.rank}</span>
            <span class="hit-main"><strong>${escapeHtml(h.title)}</strong>
              <span class="hit-meta">${escapeHtml(TYPE_LABELS[h.type] || h.type)} · cosine ${h.score_vec.toFixed(3)}${
                r.rerank ? ` · rerank ${h.score_rerank.toFixed(2)}` : ""
              }</span></span>${move}`,
        }),
        el("p", { html: body }),
      ])
    );
  });
  if (r.rerank && r.pool.length > r.hits.length) {
    const dropped = r.pool.slice(r.hits.length);
    const promoted = r.hits.filter((h) => h.rank_vec > r.k).length;
    box.append(
      el("p", {
        class: "field-hint",
        html: `${dropped.length} candidate${dropped.length === 1 ? "" : "s"} dropped by the reranker${
          promoted ? `; ${promoted} hit${promoted === 1 ? "" : "s"} came from outside the vector top-${r.k}` : ""
        }.`,
      })
    );
  }
}

// ------------------------------------------------------------------ answer with LLM

function answerPanel() {
  return el("div", { class: "panel", id: "answer-panel" }, [
    el("div", { class: "panel-h" }, ["Answer with LLM ", el("span", { class: "panel-sub" }, "OpenAI · uses your key from the API keys panel")]),
    el("div", { class: "answer-controls" }, [
      el("div", { class: "field" }, [el("span", { class: "field-label" }, "Model"), el("select", { class: "inp", id: "llm-model" })]),
      el("div", { class: "field" }, [
        el("span", { class: "field-label" }, "Knowledge mode"),
        segmented("amode", [["rag", "RAG (top-k chunks)"], ["ragless", "RAG-less (full context)"], ["both", "Compare both"]], "both"),
      ]),
      el("button", { class: "btn btn-primary", type: "button", id: "answer-go", disabled: "" }, "Answer with LLM"),
    ]),
    el("p", { class: "field-hint", id: "answer-hint" }, "Retrieve first, then ask the LLM to answer from what was found."),
    el("div", { id: "answer-error" }),
    el("div", { class: "answer-cards", id: "answer-cards" }, [answerPlaceholder()]),
    el("div", { id: "answer-compare" }),
  ]);
}

function answerPlaceholder() {
  return el("div", { class: "answer-card answer-empty" }, [
    el("div", { class: "answer-card-h" }, [el("strong", {}, "LLM answer")]),
    el("p", { class: "answer-text muted" }, "The model's reply will stream in here, word by word, with its latency and token counts underneath."),
  ]);
}

async function setupAnswer() {
  $("#answer-go").addEventListener("click", runAnswer);
  try {
    const m = await api("/api/llm/models");
    $("#llm-model").innerHTML = m.models
      .map((x) => `<option value="${x.id}" ${x.id === m.default ? "selected" : ""}>${escapeHtml(x.label)}</option>`)
      .join("");
    state.answerReady = true;
  } catch (err) {
    state.answerReady = false;
    const select = $("#llm-model");
    select.innerHTML = `<option>Unavailable</option>`;
    select.disabled = true;
    const outdated = /not found|404/i.test(err.message);
    $("#answer-error").innerHTML = `<div class="note err">${
      outdated
        ? `The backend at ${escapeHtml(apiBase() || location.origin)} is running older code without the answer feature. Stop it and start it again, then reload this page.`
        : `Could not load the model list: ${escapeHtml(err.message)}`
    }</div>`;
  }
}

function answerCard(mode) {
  const title = mode === "rag" ? "RAG · top-k chunks" : "RAG-less · full context.md";
  const card = el("div", { class: `answer-card ${mode}` }, [
    el("div", { class: "answer-card-h" }, [el("strong", {}, title), el("span", { class: "answer-state" }, "waiting…")]),
    el("p", { class: "answer-text" }),
    el("div", { class: "answer-stats" }),
  ]);
  $("#answer-cards").append(card);
  return card;
}

async function answerOnce(mode, card, model) {
  const r = state.last;
  const textEl = card.querySelector(".answer-text");
  card.querySelector(".answer-state").textContent = "streaming…";
  let done = null;
  await apiStream(
    "/api/rag/answer",
    { query: r.query, mode, model, embedder: state.embedder, chunk_ids: r.hits.map((h) => h.id) },
    (ev) => {
      if (ev.type === "delta") textEl.textContent += ev.text;
      else if (ev.type === "done") done = ev;
      else if (ev.type === "error") throw new Error(ev.message);
    }
  );
  if (!done) throw new Error("The answer stream ended early.");
  const t = done.timings_ms, u = done.usage;
  const retrieval = mode === "rag" ? r.timings_ms.total : 0;
  const firstWord = retrieval + (t.ttft || 0);
  card.querySelector(".answer-state").textContent = "done";
  const cached = u.cached_tokens ? ` (${u.cached_tokens.toLocaleString()} cached)` : "";
  const rows = [
    ...(mode === "rag" ? [["Retrieval", `${retrieval.toFixed(0)} ms`]] : []),
    ["LLM first token", `${(t.ttft ?? 0).toFixed(0)} ms`],
    ["LLM total", `${t.total.toFixed(0)} ms`],
    ["Question → first word", `${firstWord.toFixed(0)} ms`],
    ["Input tokens", `${(u.input_tokens ?? 0).toLocaleString()}${cached}`],
    ["Output tokens", `${(u.output_tokens ?? 0).toLocaleString()}`],
  ];
  card.querySelector(".answer-stats").innerHTML = rows
    .map(([k, v]) => `<div><span>${k}</span><strong>${v}</strong></div>`)
    .join("");
  return { mode, firstWord, ttft: t.ttft || 0, retrieval, input: u.input_tokens || 0, cached: u.cached_tokens || 0 };
}

async function runAnswer() {
  if (!state.last) return;
  const mode = document.querySelector('input[name="amode"]:checked').value;
  const model = $("#llm-model").value;
  const btn = $("#answer-go");
  btn.disabled = true;
  $("#answer-error").innerHTML = "";
  $("#answer-cards").innerHTML = "";
  $("#answer-compare").innerHTML = "";
  const modes = mode === "both" ? ["rag", "ragless"] : [mode];
  const cards = modes.map(answerCard);
  const results = [];
  try {
    // One after the other so the two latency measurements don't compete for the network.
    for (let i = 0; i < modes.length; i++) results.push(await answerOnce(modes[i], cards[i], model));
    if (results.length === 2) paintCompare(results[0], results[1]);
  } catch (err) {
    const box = el("div", { class: "note err" }, err.message);
    if (/key/i.test(err.message)) {
      box.append(" ", el("a", { href: "#keys-mount", onclick: () => ($(".keys-panel").open = true) }, "Open the API keys panel."));
    }
    $("#answer-error").append(box);
    cards.forEach((c) => {
      const s = c.querySelector(".answer-state");
      if (s.textContent !== "done") s.textContent = "failed";
    });
  } finally {
    btn.disabled = false;
  }
}

function paintCompare(rag, ragless) {
  const overhead = rag.firstWord - ragless.firstWord;
  const verdict =
    overhead > 0
      ? `RAG added <strong>${overhead.toFixed(0)} ms</strong> before the first word`
      : `RAG was <strong>${(-overhead).toFixed(0)} ms faster</strong> to the first word`;
  const ratio = (ragless.input / Math.max(rag.input, 1)).toFixed(0);
  const cache = ragless.cached ? `, ${ragless.cached.toLocaleString()} of them served from OpenAI's prompt cache` : "";
  $("#answer-compare").innerHTML = `<div class="note">${verdict}: retrieval ${rag.retrieval.toFixed(0)} ms + first token ${rag.ttft.toFixed(0)} ms,
    versus ${ragless.ttft.toFixed(0)} ms for RAG-less. RAG-less sent ${ratio}× more input tokens
    (${ragless.input.toLocaleString()} vs ${rag.input.toLocaleString()})${cache}.
    Run it again: the RAG-less prompt is usually cached from the second call on.</div>`;
}

// ------------------------------------------------------------------ plot

function groupOf(p) {
  if (state.colorBy === "type") return { key: p.type, label: TYPE_LABELS[p.type] || p.type };
  if (!p.category_id) return { key: "_general", label: "Company & policies" };
  const cat = state.catalog.categories.find((c) => c.id === p.category_id);
  return { key: p.category_id, label: cat ? cat.name : p.category_id };
}

function groupOrder() {
  if (state.colorBy === "type") return Object.keys(TYPE_LABELS);
  return [...state.catalog.categories.map((c) => c.id), "_general"];
}

function drawPlot() {
  const Plotly = window.Plotly;
  const node = $("#rag-plot");
  if (!Plotly || !state.points.length) return;
  const is3d = state.dim === 3;
  const r = state.last && state.last.query_xyz.length === state.dim ? state.last : null;
  const hitIdx = new Map(r ? r.hits.map((h) => [h.idx, h.rank]) : []);
  const type = is3d ? "scatter3d" : "scattergl";

  const order = groupOrder();
  const groups = new Map(order.map((k) => [k, []]));
  state.points.forEach((p) => {
    const g = groupOf(p);
    if (!groups.has(g.key)) groups.set(g.key, []);
    groups.get(g.key).push(p);
  });

  const traces = [];
  let slot = 0;
  for (const [key, pts] of groups) {
    if (!pts.length) continue;
    const color = key === "_general" ? NEUTRAL : SERIES[slot++ % SERIES.length];
    const label = groupOf(pts[0]).label;
    traces.push({
      type,
      mode: "markers",
      name: label,
      x: pts.map((p) => p.xyz[0]),
      y: pts.map((p) => p.xyz[1]),
      ...(is3d ? { z: pts.map((p) => p.xyz[2]) } : {}),
      customdata: pts.map((p) => [p.title, TYPE_LABELS[p.type] || p.type, p.product || "Company-wide", p.snippet]),
      hovertemplate: "<b>%{customdata[0]}</b><br>%{customdata[1]} · %{customdata[2]}<br><span style='font-size:11px'>%{customdata[3]}</span><extra></extra>",
      marker: {
        color,
        size: is3d ? 4 : 8,
        opacity: is3d && r ? 0.35 : 0.9,
        line: is3d ? undefined : { color: "#ffffff", width: 1 },
      },
      // Dim non-hits through selection so the legend swatches keep full color.
      ...(r && !is3d
        ? {
            selectedpoints: pts.map((p, i) => (hitIdx.has(p.idx) ? i : -1)).filter((i) => i >= 0),
            selected: { marker: { opacity: 1 } },
            unselected: { marker: { opacity: 0.3 } },
          }
        : {}),
    });
  }

  if (r) {
    const hits = r.hits.map((h) => state.points[h.idx]);
    const q = r.query_xyz;
    // Spokes from the query to each hit; null breaks the line between segments.
    const xs = [], ys = [], zs = [];
    hits.forEach((p) => {
      xs.push(q[0], p.xyz[0], null);
      ys.push(q[1], p.xyz[1], null);
      zs.push(q[2], p.xyz[2], null);
    });
    traces.push({
      type: is3d ? "scatter3d" : "scatter",
      mode: "lines",
      name: "Query → hits",
      x: xs, y: ys, ...(is3d ? { z: zs } : {}),
      line: { color: "rgba(11,16,32,.35)", width: 2 },
      hoverinfo: "skip",
      showlegend: false,
    });
    traces.push({
      type: is3d ? "scatter3d" : "scatter",
      mode: "markers+text",
      name: `Top-${r.k} hits`,
      x: hits.map((p) => p.xyz[0]),
      y: hits.map((p) => p.xyz[1]),
      ...(is3d ? { z: hits.map((p) => p.xyz[2]) } : {}),
      text: r.hits.map((h) => String(h.rank)),
      textposition: "top center",
      textfont: { color: "#0b1020", size: 12, family: "Inter" },
      customdata: r.hits.map((h) => [h.title, h.rank, h.score_vec.toFixed(3)]),
      hovertemplate: "<b>#%{customdata[1]} %{customdata[0]}</b><br>cosine %{customdata[2]}<extra></extra>",
      marker: { size: is3d ? 7 : 14, color: "rgba(0,0,0,0)", line: { color: "#0b1020", width: 2 } },
    });
    traces.push({
      type: is3d ? "scatter3d" : "scatter",
      mode: "markers",
      name: "Your query",
      x: [q[0]], y: [q[1]], ...(is3d ? { z: [q[2]] } : {}),
      hovertemplate: `<b>Query</b><br>${escapeHtml(r.query)}<extra></extra>`,
      marker: { symbol: is3d ? "diamond" : "star", size: is3d ? 9 : 20, color: "#0b1020", line: { color: "#ffffff", width: 2 } },
    });
  }

  const axis = { showgrid: false, zeroline: false, showticklabels: false, title: "" };
  const layout = {
    margin: { l: 8, r: 8, t: 8, b: 8 },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "Inter, system-ui, sans-serif", color: "#475569", size: 12 },
    legend: { orientation: "h", y: -0.02, yanchor: "top", font: { size: 11 } },
    hoverlabel: { bgcolor: "#111827", bordercolor: "#111827", font: { color: "#f1f5f9", size: 12 }, align: "left" },
    uirevision: `${state.dim}-${state.colorBy}-${state.embedder}`, // keep zoom/camera across re-renders
    ...(is3d
      ? { scene: { xaxis: { ...axis, showbackground: false }, yaxis: { ...axis, showbackground: false }, zaxis: { ...axis, showbackground: false } } }
      : { xaxis: axis, yaxis: axis }),
  };
  Plotly.react(node, traces, layout, { displaylogo: false, responsive: true, modeBarButtonsToRemove: ["select2d", "lasso2d"] });
}

