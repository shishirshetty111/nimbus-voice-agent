// agent.html: the finished Nimbus voice agent on its own page, with the live cart beside it.

import { el, escapeHtml, icon } from "../app.js";
import { NimbusAgent } from "./agent-core.js";
import { api } from "./api.js";
import { ConvoView } from "./convo.js";
import { mountKeysPanel } from "./keys.js";
import { STAGES, fmt } from "./latency-model.js";

const $ = (sel, root = document) => root.querySelector(sel);
const STATE = {
  off: ["", "Tap the mic and start talking"],
  connecting: ["dot-thinking", "Connecting…"],
  listening: ["dot-listening", "Listening…"],
  hearing: ["dot-listening", "Hearing you…"],
  thinking: ["dot-thinking", "Thinking…"],
  speaking: ["dot-speaking", "Speaking… (just talk to interrupt)"],
};
const EXAMPLES = [
  "Which HR products do you have?",
  "What does Nimbus CRM Professional cost for 5 users?",
  "How much would I save paying annually?",
  "Add Nimbus Books Starter to my cart",
  "What's your refund policy?",
  "What's in my cart?",
];
let agent, convo;

export async function renderAgent() {
  document.querySelector("#app").append(
    el("section", { class: "console-hero agent-hero" }, [
      el("div", { class: "container" }, [
        el("div", { class: "eyebrow" }, "Nimbus assistant"),
        el("h1", {}, "Talk to Nimbus"),
        el("p", { class: "lead" }, "Ask about products, plans and policies, or have the assistant build your cart and check out. Speak or type; talk over it any time to interrupt."),
        el("div", { class: "agent-meta" }, [
          el("span", { class: "pill", id: "a-preset" }, [el("span", { class: "dot" }), el("span", {}, "Loading…")]),
          el("label", { class: "check agent-fast" }, [el("input", { type: "checkbox", id: "a-fast" }), " ⚡ Low-latency mode"]),
          el("a", { href: "playground.html", class: "agent-tune" }, "Tune the agent in the Playground →"),
        ]),
      ]),
    ]),
    el("section", { class: "section", style: "padding-top:28px" }, [
      el("div", { class: "container agent-grid" }, [
        el("div", { class: "panel agent-main" }, [
          el("div", { class: "agent-stage" }, [
            el("button", { class: "mic-btn agent-mic", id: "a-mic", type: "button", "aria-label": "Start talking", html: icon("mic") }),
            el("div", { class: "agent-status-wrap" }, [
              el("div", { class: "talk-status", id: "a-status" }, [el("span", {}), el("span", {}, STATE.off[1])]),
              el("div", { class: "viz-level" }, [el("i", { id: "a-level", style: "width:0%" })]),
              el("div", { class: "agent-partial", id: "a-partial" }),
            ]),
          ]),
          el("div", { class: "talk-log agent-log", id: "a-log", "aria-live": "polite" }),
          el("form", { class: "try-row", id: "a-form" }, [
            el("input", { class: "inp", id: "a-input", placeholder: "Or type a message…", autocomplete: "off", maxlength: "2000" }),
            el("button", { class: "btn btn-primary", type: "submit" }, "Send"),
          ]),
          el("div", { class: "agent-foot" }, [
            el("button", { class: "linkish", type: "button", id: "a-new" }, "New conversation"),
            el("span", { class: "field-hint" }, "Headphones work best for voice."),
          ]),
          el("div", { id: "a-keys" }),
        ]),
        el("aside", { class: "agent-side" }, [
          el("div", { class: "panel" }, [
            el("div", { class: "panel-h agent-side-h" }, ["Your cart ", el("button", { class: "linkish", type: "button", onclick: () => window.nimbusCart?.open() }, "open")]),
            el("div", { id: "a-cart" }),
          ]),
          el("div", { class: "panel" }, [
            el("div", { class: "panel-h" }, "Try saying"),
            el("div", { class: "tag-row" }, EXAMPLES.map((q) => el("button", { class: "tag tag-btn", type: "button", onclick: () => send(q) }, q))),
          ]),
          el("div", { class: "panel" }, [
            el("div", { class: "panel-h" }, ["Last reply ", el("span", { class: "panel-sub", id: "a-lat-sub" }, "")]),
            el("div", { id: "a-lat" }, [el("p", { class: "field-hint" }, "Where the time goes appears here after your first question.")]),
          ]),
        ]),
      ]),
    ])
  );

  convo = new ConvoView($("#a-log"));
  agent = new NimbusAgent({
    onState: paintState,
    onLevel: (x) => ($("#a-level").style.width = `${Math.round(x * 100)}%`),
    onPartial: (t) => ($("#a-partial").textContent = t),
    onUser: (t) => convo.user(t),
    onAgentStart: () => convo.agentStart(),
    onAgentEvent: (ev) => convo.onEvent(ev),
    onBargeIn: () => convo.bargeIn(),
    onInterrupted: (ev) => convo.interrupted(ev),
    onTurn: (rec) => (convo.latency(rec), convo.finish(), paintLatency(rec)),
    onCart: paintCart,
    onError: (m) => convo.onEvent({ type: "error", message: m }) || flash(m),
  });

  const preset = await agent.init();
  $("#a-preset").lastChild.textContent = preset.summary;
  $("#a-fast").checked = preset.fast;
  $("#a-fast").addEventListener("change", () => {
    $("#a-preset").lastChild.textContent = agent.setFast($("#a-fast").checked).summary;
  });
  if (!preset.canText) {
    $("#a-preset").className = "pill pill-bad";
    $("#a-keys").append(el("p", { class: "note" }, "Add an API key to start. OpenAI covers everything (voice in, thinking, voice out); Gemini or Anthropic work for thinking."));
    mountKeysPanel($("#a-keys"), {});
  }
  if (!preset.canVoice) $("#a-mic").disabled = true;

  $("#a-mic").addEventListener("click", () => agent.toggleVoice());
  $("#a-form").addEventListener("submit", (e) => {
    e.preventDefault();
    send($("#a-input").value);
  });
  $("#a-new").addEventListener("click", async () => {
    await agent.reset();
    convo.empty("New conversation. Your cart is kept.");
  });
  window.addEventListener("nimbus:cart-changed", paintCart);
  window.addEventListener("focus", paintCart);

  const past = await agent.history();
  if (past.length) convo.history(past);
  else convo.empty("Hi! I'm the Nimbus assistant. Ask me anything about our products, or tap the mic to talk.");
  paintCart();
}

function send(text) {
  text = (text || "").trim();
  if (!text || !agent.preset.canText) return;
  $("#a-input").value = "";
  agent.sendText(text);
}

function flash(msg) {
  $("#a-partial").textContent = msg;
}

function paintState(s) {
  const [dot, label] = STATE[s] || STATE.off;
  $("#a-status").innerHTML = `<span class="${dot}"></span><span>${label}</span>`;
  $("#a-mic").classList.toggle("live", s !== "off");
  $("#a-mic").setAttribute("aria-label", s === "off" ? "Start talking" : "Stop talking");
  if (s === "listening") $("#a-partial").textContent = "";
}

async function paintCart() {
  const box = $("#a-cart");
  try {
    const c = await api("/api/cart");
    if (!c.lines.length) {
      box.innerHTML = `<p class="field-hint">Empty. Try “add Nimbus CRM Starter for 3 users”.</p>`;
      return;
    }
    box.innerHTML = `<ul class="mini-cart">${c.lines.map((l) => `<li><span><strong>${escapeHtml(l.product_name)}</strong>
      ${escapeHtml(l.tier)} · ${l.seats} seat${l.seats === 1 ? "" : "s"} · ${l.billing}</span><b>$${(l.unit_price * l.seats).toLocaleString()}/mo</b></li>`).join("")}</ul>
      <div class="mini-total"><span>Per month</span><strong>$${c.totals.per_month.toLocaleString()}</strong></div>
      <div class="mini-total sub"><span>Due today</span><span>$${c.totals.due_today.toLocaleString()}</span></div>`;
  } catch {
    box.innerHTML = `<p class="field-hint">Cart unavailable (backend offline).</p>`;
  }
}

function paintLatency(rec) {
  if (!rec) return;
  const items = STAGES.map((s) => ({ ...s, ms: rec.stages[s.key] || 0 })).filter((s) => s.ms >= 0.5);
  const top = [...items].sort((a, b) => b.ms - a.ms)[0];
  $("#a-lat-sub").textContent = rec.kind === "text" ? "to the first word" : "you stopped → you heard it";
  $("#a-lat").innerHTML = `
    <div class="mini-lat-total">${fmt(rec.total)}</div>
    <div class="mini-lat-bar">${items.map((s) => `<span style="flex:${s.ms};background:${s.color}" title="${escapeHtml(s.label)}: ${fmt(s.ms)}"></span>`).join("")}</div>
    <ul class="mini-lat-list">${[...items].sort((a, b) => b.ms - a.ms).slice(0, 4).map((s) =>
      `<li><i style="background:${s.color}"></i>${escapeHtml(s.label)}<span>${fmt(s.ms)} · ${Math.round((s.ms / rec.total) * 100)}%</span></li>`).join("")}</ul>
    ${top ? `<p class="field-hint">Biggest share: ${escapeHtml(top.label)}. Full breakdown in the Playground's Latency panel.</p>` : ""}`;
}

