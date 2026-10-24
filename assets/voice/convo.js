// Conversation log for the agent page and the landing widget: user / agent bubbles, tool and
// retrieval chips, barge-in display, and a one-line latency note per reply.

import { el } from "../app.js";
import { fmt } from "./latency-model.js";

const TOOL_NAMES = {
  search_knowledge: "Looked it up", add_to_cart: "Added to cart", remove_from_cart: "Removed from cart",
  clear_cart: "Cleared cart", get_cart_total: "Checked cart", checkout: "Checked out", checkout_item: "Checked out item",
  annual_price: "Priced annual plan", annual_savings: "Worked out savings", sort_products_by_price: "Sorted by price",
  top_k_expensive: "Ranked by price",
};

export class ConvoView {
  constructor(log, { compact = false } = {}) {
    this.log = log;
    this.compact = compact;
    this.agent = null;
    this.cut = null;
  }

  scroll() {
    this.log.scrollTop = this.log.scrollHeight;
  }

  empty(text) {
    this.log.replaceChildren(el("p", { class: "muted convo-empty" }, text));
  }

  row(role, text = "") {
    this.log.querySelector(".convo-empty")?.remove();
    const bubble = el("div", { class: "bubble" }, text);
    const col = el("div", { class: "chat-col" }, [bubble]);
    this.log.append(el("div", { class: `chat ${role}` }, [col]));
    this.scroll();
    return { bubble, col };
  }

  user(text) {
    this.row("user", text);
  }

  // Start an agent reply; events then go to onEvent().
  agentStart() {
    this.finish();
    const { bubble, col } = this.row("agent");
    bubble.classList.add("typing");
    const trace = el("div", { class: "trace" });
    col.append(trace);
    this.agent = { bubble, col, trace, text: "" };
  }

  onEvent(ev) {
    const a = this.agent;
    if (!a) return;
    if (ev.type === "delta") {
      a.bubble.classList.remove("typing");
      a.text += ev.text;
      a.bubble.textContent = a.text;
    } else if (ev.type === "tool") {
      const label = `${ev.ok ? "✓" : "✗"} ${TOOL_NAMES[ev.name] || ev.name}${ev.parallel ? " ∥" : ""}`;
      a.trace.append(this.compact ? el("span", { class: `chip-tool ${ev.ok ? "" : "bad"}` }, label)
        : el("details", { class: `tool-call ${ev.ok ? "ok" : "bad"}` }, [
            el("summary", {}, `${label} · ${ev.name}(${Object.entries(ev.args || {}).map(([k, v]) => `${k}: ${v}`).join(", ")})`),
            el("pre", { class: "json" }, JSON.stringify(ev.result, null, 2)),
          ]));
    } else if (ev.type === "rag") {
      a.trace.append(el("span", { class: "chip-tool" }, `Searched the knowledge base (${ev.hits.length})`));
    } else if (ev.type === "error") {
      a.col.append(el("div", { class: "note err chat-err" }, ev.message));
    }
    this.scroll();
  }

  latency(rec) {
    if (!this.agent || !rec) return;
    const what = rec.kind === "text" ? "first word" : "you stopped → reply";
    this.agent.col.append(el("div", { class: "latency-row" }, `${what} in ${fmt(rec.total)}`));
    this.scroll();
  }

  bargeIn() {
    this.cut = this.agent;
    this.finish();
  }

  interrupted({ spoken, reply, played_ms }) {
    const a = this.cut || this.agent;
    this.cut = null;
    if (!a) return;
    a.bubble.classList.remove("typing");
    if (!a.bubble.isConnected) a.col.prepend(a.bubble);
    const rest = reply.startsWith(spoken) ? reply.slice(spoken.length) : reply;
    a.bubble.replaceChildren(el("span", {}, spoken), el("span", { class: "unheard" }, rest));
    a.col.append(el("div", { class: "note interrupted-note" }, `You interrupted after ${(played_ms / 1000).toFixed(1)} s.`));
    this.scroll();
  }

  finish() {
    const a = this.agent;
    if (a) {
      a.bubble.classList.remove("typing");
      if (!a.bubble.textContent) a.bubble.remove();
    }
    this.agent = null;
  }

  history(transcript) {
    for (const t of transcript) {
      this.user(t.user);
      const { bubble } = this.row("agent", t.assistant.split("\n[Actions taken")[0].replace(" [cancelled by the user]", ""));
      if (t.assistant.includes("[cancelled by the user]")) bubble.classList.add("was-cut");
    }
  }
}
