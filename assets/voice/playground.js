// Playground: every knob of the voice agent in one place. M3 ships the Tools section:
// choose which tools the agent may use (all / none / any subset) and run any tool by hand.

import { el, escapeHtml } from "../app.js";
import { getCartId } from "./cart-id.js";
import { api, apiBase } from "./api.js";
import { chatPanels, setupChat } from "./chat-panel.js";
import { setupVoice, voicePanel } from "./voice-panel.js";
import { dashboardPanel, setupDashboard } from "./dashboard.js";
import { keyHeaders, mountKeysPanel } from "./keys.js";
import { getSettings, setSettings } from "./settings.js";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { tools: [] };

export async function renderPlayground() {
  document.querySelector("#app").append(
    el("section", { class: "console-hero" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "Playground"),
        el("h1", {}, "Tune the Nimbus voice agent"),
        el("p", {
          class: "lead",
          html: "Every setting the agent uses lives here: its LLM, knowledge, memory and tools. Type or talk to it below to see each turn's tool calls and latency.",
        }),
        el("div", { id: "backend-pill", class: "pill" }, [el("span", { class: "dot" }), el("span", {}, "Connecting to backend…")]),
      ]),
    ]),
    el("section", { class: "section", style: "padding-top:32px" }, [
      el("div", { class: "container page" }, [
        el("div", { id: "pg-error" }),
        el("div", { id: "keys-mount" }),
        ...chatPanels(),
        voicePanel(),
        dashboardPanel(),
        toolsPanel(),
        testerPanel(),
      ]),
    ])
  );
  await connect();
}

// ------------------------------------------------------------------ panels

function toolsPanel() {
  return el("div", { class: "panel" }, [
    el("div", { class: "panel-h tools-h" }, [
      el("span", {}, ["Agent tools ", el("span", { class: "panel-sub", id: "tools-count" }, "")]),
      el("span", { class: "tools-bulk" }, [
        el("button", { class: "btn btn-ghost btn-sm", type: "button", id: "tools-all" }, "Enable all"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", id: "tools-none" }, "Disable all"),
      ]),
    ]),
    el("p", { class: "field-hint" }, "Only checked tools are offered to the LLM. With none checked, the agent can only talk; it cannot change the cart or compute prices."),
    el("div", { id: "tools-groups" }),
  ]);
}

function testerPanel() {
  return el("div", { class: "panel" }, [
    el("div", { class: "panel-h" }, ["Try a tool ", el("span", { class: "panel-sub", id: "cart-id" }, "")]),
    el("div", { class: "tester" }, [
      el("div", { class: "field" }, [el("span", { class: "field-label" }, "Tool"), el("select", { class: "inp", id: "t-tool" })]),
      el("p", { class: "field-hint", id: "t-desc" }, ""),
      el("form", { id: "t-form", class: "tester-form" }),
      el("div", { class: "try-row" }, [
        el("button", { class: "btn btn-primary", type: "submit", form: "t-form", id: "t-run" }, "Run tool"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: openDrawer }, "Open cart"),
      ]),
    ]),
    el("div", { id: "t-out" }),
  ]);
}

function openDrawer() {
  // The nav's cart icon opens the drawer in every version of the cart script.
  if (window.nimbusCart) window.nimbusCart.open();
  else document.querySelector(".cart-btn")?.click();
}

// ------------------------------------------------------------------ data

async function connect() {
  const pill = $("#backend-pill");
  let serverKeys = null;
  try {
    const [t, keys] = await Promise.all([api("/api/tools"), api("/api/keys/status")]);
    state.tools = t.tools;
    serverKeys = keys;
    pill.lastChild.textContent = `Backend connected · ${apiBase() || location.origin}`;
  } catch (err) {
    pill.className = "pill pill-bad";
    pill.lastChild.textContent = "Backend offline";
    $("#pg-error").innerHTML = `<div class="note err">${escapeHtml(err.message)}</div>`;
  }
  mountKeysPanel($("#keys-mount"), serverKeys);
  if (!state.tools.length) return;
  paintToggles();
  setupTester();
  try {
    await setupChat(serverKeys);
    await setupVoice();
    setupDashboard();
  } catch (err) {
    $("#pg-error").innerHTML = `<div class="note err">${escapeHtml(err.message)}</div>`;
  }
}

function enabledSet() {
  const saved = getSettings().tools_enabled;
  return new Set(saved === null ? state.tools.map((t) => t.name) : saved);
}

function paintToggles() {
  const on = enabledSet();
  const groups = {};
  state.tools.forEach((t) => (groups[t.group] ||= []).push(t));
  const box = $("#tools-groups");
  box.innerHTML = "";
  for (const [group, list] of Object.entries(groups)) {
    box.append(
      el("div", { class: "tool-group" }, [
        el("div", { class: "tool-group-h" }, group),
        el(
          "div",
          { class: "tool-toggles" },
          list.map((t) =>
            el("label", { class: "tool-toggle" }, [
              el("input", { type: "checkbox", value: t.name, ...(on.has(t.name) ? { checked: "" } : {}) }),
              el("span", {}, [el("strong", {}, t.name), el("em", {}, firstSentence(t.description))]),
            ])
          )
        ),
      ])
    );
  }
  box.querySelectorAll("input[type=checkbox]").forEach((c) => c.addEventListener("change", saveToggles));
  $("#tools-all").onclick = () => setAll(true);
  $("#tools-none").onclick = () => setAll(false);
  countToggles();
}

function saveToggles() {
  const names = [...document.querySelectorAll("#tools-groups input:checked")].map((c) => c.value);
  setSettings({ tools_enabled: names.length === state.tools.length ? null : names });
  countToggles();
  window.dispatchEvent(new Event("nimbus:tools-changed"));
}

function setAll(on) {
  document.querySelectorAll("#tools-groups input[type=checkbox]").forEach((c) => (c.checked = on));
  saveToggles();
}

function countToggles() {
  const n = enabledSet().size;
  $("#tools-count").textContent = n === state.tools.length ? `all ${n} enabled` : n === 0 ? "none enabled" : `${n} of ${state.tools.length} enabled`;
}

const firstSentence = (s) => s.split(/(?<=\.)\s/)[0];

// ------------------------------------------------------------------ tester

function setupTester() {
  $("#cart-id").textContent = `cart ${getCartId()} · shared with the cart drawer`;
  $("#t-tool").innerHTML = state.tools.map((t) => `<option value="${t.name}">${t.name}</option>`).join("");
  $("#t-tool").addEventListener("change", paintForm);
  $("#t-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runTool();
  });
  paintForm();
}

function paintForm() {
  const tool = state.tools.find((t) => t.name === $("#t-tool").value);
  $("#t-desc").textContent = tool.description;
  const form = $("#t-form");
  form.innerHTML = "";
  const props = tool.parameters.properties || {};
  const required = new Set(tool.parameters.required || []);
  if (!Object.keys(props).length) form.append(el("p", { class: "muted", style: "font-size:.88rem" }, "This tool takes no arguments."));
  for (const [name, spec] of Object.entries(props)) {
    const label = `${name}${required.has(name) ? " *" : ""}`;
    let input;
    if (spec.enum) {
      input = el("select", { class: "inp", name }, [
        ...(required.has(name) ? [] : [el("option", { value: "" }, "(default)")]),
        ...spec.enum.map((v) => el("option", { value: v }, v)),
      ]);
    } else {
      input = el("input", {
        class: "inp",
        name,
        type: spec.type === "integer" ? "number" : "text",
        ...(spec.minimum != null ? { min: spec.minimum } : {}),
        ...(spec.maximum != null ? { max: spec.maximum } : {}),
        ...(required.has(name) ? { required: "" } : {}),
        placeholder: spec.type === "integer" ? "default" : "",
      });
    }
    form.append(el("label", { class: "field" }, [el("span", { class: "field-label" }, label), input, el("span", { class: "field-hint" }, spec.description || "")]));
  }
}

async function runTool() {
  const name = $("#t-tool").value;
  const tool = state.tools.find((t) => t.name === name);
  const args = {};
  new FormData($("#t-form")).forEach((v, k) => {
    if (v === "") return;
    args[k] = tool.parameters.properties[k].type === "integer" ? Number(v) : v;
  });
  const btn = $("#t-run");
  btn.disabled = true;
  try {
    const res = await fetch(apiBase() + `/api/tools/${name}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Cart-Id": getCartId(), ...keyHeaders() },
      body: JSON.stringify(args),
    });
    const out = await res.json();
    if (!res.ok) throw new Error(out.detail || `HTTP ${res.status}`);
    $("#t-out").innerHTML = `
      <div class="tool-result ${out.ok ? "ok" : "bad"}">
        <span class="tool-status">${out.ok ? "OK" : "Error"}</span>
        <span class="muted"><code>${escapeHtml(name)}(${escapeHtml(JSON.stringify(args))})</code></span>
        <span class="tool-ms">${out.ms.toFixed(1)} ms</span>
      </div>
      <pre class="json">${escapeHtml(JSON.stringify(out.result, null, 2))}</pre>`;
    if (out.changes_cart) window.dispatchEvent(new Event("nimbus:cart-changed"));
  } catch (err) {
    $("#t-out").innerHTML = `<div class="note err">${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}
