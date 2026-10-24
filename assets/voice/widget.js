// "Talk to Nimbus" chat box for the landing page: a floating button that opens a compact chat with
// voice. Same agent, conversation and cart as agent.html. It contacts the backend only once opened.
// Visitors paste their own API key right here (kept in this browser's localStorage); any element
// with a data-open-assistant attribute opens the box too.

import { el, escapeHtml, icon } from "../app.js";
import { NimbusAgent } from "./agent-core.js";
import { ConvoView } from "./convo.js";
import { PROVIDERS, loadKeys, setKey } from "./keys.js";

const STATE = {
  off: "Type, or tap the mic to talk",
  listening: "Listening…",
  hearing: "Hearing you…",
  thinking: "Thinking…",
  speaking: "Speaking… talk to interrupt",
};

export function mountWidget() {
  if (document.querySelector(".voice-fab")) return;
  if (!document.querySelector('link[href$="voice/voice.css"]')) {
    document.head.append(el("link", { rel: "stylesheet", href: new URL("./voice.css", import.meta.url).href }));
  }
  const fab = el("button", { class: "voice-fab", type: "button", "aria-label": "Chat with the Nimbus assistant", html: icon("chat") });
  const panel = el("div", { class: "voice-panel nimbus-widget", role: "dialog", "aria-label": "Nimbus assistant" }, [
    el("header", { html: `<span>${icon("sparkle")} Nimbus assistant</span>` }, [
      el("span", { class: "w-head-actions" }, [
        el("button", { class: "w-keys-btn", type: "button", html: `${icon("lock")} Keys`, onclick: () => showKeys(!keysOpen) }),
        el("button", { class: "vp-x", type: "button", "aria-label": "Close", html: icon("arrow"), onclick: () => toggle(false) }),
      ]),
    ]),
    el("div", { class: "vp-status" }, [el("span", { class: "w-state" }, "Connecting…"), el("span", { class: "w-partial" })]),
    el("div", { class: "vp-log", "aria-live": "polite" }),
    keyForm(),
    el("form", { class: "vp-controls" }, [
      el("button", { class: "vp-mic", type: "button", "aria-label": "Talk", html: icon("mic") }),
      el("input", { class: "inp", placeholder: "Ask about products, prices…", autocomplete: "off", maxlength: "2000" }),
      el("button", { class: "btn btn-primary btn-sm", type: "submit" }, "Send"),
    ]),
    el("a", { class: "vp-link", href: "agent.html" }, "Open the full assistant →"),
  ]);
  document.body.append(fab, panel);

  const $ = (sel) => panel.querySelector(sel);
  const convo = new ConvoView($(".vp-log"), { compact: true });
  let agent, ready, keysOpen = false, greeted = false;

  function showKeys(open) {
    keysOpen = open;
    $(".w-keys").hidden = !open;
    $(".vp-log").hidden = open;
    $(".vp-controls").hidden = open;
    $(".w-keys-btn").classList.toggle("on", open);
    if (open) {
      for (const inp of panel.querySelectorAll(".w-keys input")) inp.value = loadKeys()[inp.name] || "";
      setTimeout(() => $(".w-keys input").focus(), 50);
    }
  }

  // Keys changed: pick providers again. A live voice session is stopped so the next one uses them.
  async function applyKeys() {
    for (const inp of panel.querySelectorAll(".w-keys input")) setKey(inp.name, inp.value);
    if (agent?.voiceOn) await agent.toggleVoice();
    await configure();
  }

  async function configure() {
    const preset = await agent.init();
    if (!preset.canText) {
      $(".w-state").textContent = "Add an API key to start.";
      showKeys(true);
      return;
    }
    showKeys(false);
    $(".w-state").textContent = preset.canVoice ? STATE.off : "Type a message (voice needs a speech API key)";
    $(".vp-mic").disabled = !preset.canVoice;
    if (greeted) return;
    greeted = true;
    const past = await agent.history();
    if (past.length) convo.history(past.slice(-6));
    else convo.empty("Hi! Ask me about Nimbus products, plans or policies. I can also build your cart.");
  }

  async function start() {
    agent = new NimbusAgent({
      onState: (s) => {
        $(".w-state").textContent = STATE[s] || STATE.off;
        $(".vp-mic").classList.toggle("live", s !== "off");
        if (s === "listening") $(".w-partial").textContent = "";
      },
      onPartial: (t) => ($(".w-partial").textContent = t ? ` · “${t}”` : ""),
      onUser: (t) => convo.user(t),
      onAgentStart: () => convo.agentStart(),
      onAgentEvent: (ev) => convo.onEvent(ev),
      onBargeIn: () => convo.bargeIn(),
      onInterrupted: (ev) => convo.interrupted(ev),
      onTurn: (rec) => (convo.latency(rec), convo.finish()),
      onError: (m) => ($(".w-state").textContent = m),
    });
    await configure();
  }

  function toggle(open = !panel.classList.contains("open")) {
    panel.classList.toggle("open", open);
    fab.setAttribute("aria-expanded", String(open));
    if (open && !ready) ready = start();
    if (open && !keysOpen) setTimeout(() => $(".vp-controls input").focus(), 50);
  }

  fab.addEventListener("click", () => toggle());
  document.addEventListener("click", (e) => {
    if (e.target.closest?.("[data-open-assistant]")) {
      e.preventDefault();
      toggle(true);
    }
  });
  $(".w-keys").addEventListener("submit", async (e) => {
    e.preventDefault();
    await ready;
    await applyKeys();
  });
  $(".vp-mic").addEventListener("click", async () => {
    await ready;
    agent?.toggleVoice();
  });
  // Always address the chat form and its input by class: the panel also holds the key form, whose
  // inputs come first (a bare "form"/"input" lookup once sent a pasted API key as a chat message).
  $(".vp-controls").addEventListener("submit", async (e) => {
    e.preventDefault();
    await ready;
    const input = $(".vp-controls input");
    const text = input.value.trim();
    if (!text || !agent?.preset?.canText) return;
    input.value = "";
    agent.sendText(text);
  });
}


// "Bring your own key" form. One OpenAI key is enough for everything; the others are optional.
function keyForm() {
  const field = (p, hint) =>
    el("label", { class: "w-key" }, [
      el("span", { html: `<b>${escapeHtml(p.label)}</b> <em>${escapeHtml(hint || p.uses)}</em>` }),
      el("input", { class: "inp mono", type: "password", name: p.id, placeholder: p.placeholder, autocomplete: "off", spellcheck: "false" }),
    ]);
  const [openai, ...others] = PROVIDERS;
  const form = el("form", { class: "w-keys" }, [
    el("p", {
      class: "w-keys-intro",
      html: "<strong>Bring your own API key.</strong> The assistant runs on your own AI account. " +
        "Your key is saved only in this browser and sent only with your own requests; the server never stores it.",
    }),
    field(openai, "one key covers listening, thinking and speaking"),
    el("details", { class: "w-more" }, [el("summary", {}, "Other providers (optional)"), ...others.map((p) => field(p))]),
    el("button", { class: "btn btn-primary btn-sm", type: "submit" }, "Save and start"),
    el("p", {
      class: "w-keys-help",
      html: 'Get an OpenAI key at <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com/api-keys</a>. ' +
        'Want to tune models, voices and latency? Try the <a href="playground.html">Playground</a>.',
    }),
  ]);
  form.hidden = true;
  return form;
}
