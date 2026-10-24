"""One text chat turn: knowledge (RAG / RAG-less), prompt, provider tool loop, memory, timings."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator

from pydantic import BaseModel, Field

from backend import latency_profile, prompts
from backend.config import PRODUCTION
from backend.llm import PROVIDERS
from backend.llm.base import TurnRequest, ms_since
from backend.memory import Turn, memory
from backend.tools import registry

log = logging.getLogger("nimbus")

# Anything shaped like a provider API key. A key pasted into the chat by mistake must not reach the
# LLM, the conversation memory (Redis) or the logs, so it is replaced before anything else happens.
KEY_SHAPE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|sk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{30,})")
KEY_REDACTED = "[API key removed]"


def redact_keys(text: str) -> str:
    return KEY_SHAPE.sub(KEY_REDACTED, text)


class ChatSettings(BaseModel):
    provider: str = "openai"
    model: str | None = None                 # None = the provider's default
    persona: str = Field("", max_length=6000)  # "" = default persona
    length: str = Field("medium", pattern="^(low|medium|high)$")
    verbatim_turns: int = Field(6, ge=0, le=30)
    knowledge_mode: str = Field("rag", pattern="^(rag|ragless)$")
    rag_k: int = Field(5, ge=1, le=10)
    rerank: bool = False                     # off by default: no recall gain on our eval, +140-400 ms
    tools_enabled: list[str] | None = None   # None = all tools, [] = none
    latency_mode: str = Field("normal", pattern="^(normal|fast)$")  # "fast" = Low-latency mode profile


def effective(s: ChatSettings) -> ChatSettings:
    """Settings after Low-latency mode overrides (unchanged in normal mode)."""
    return s.model_copy(update=latency_profile.OVERRIDES) if s.latency_mode == latency_profile.FAST else s


def resolve(settings: ChatSettings, message: str | None = None, sticky: set[str] | None = None) -> tuple[object, str, list]:
    """Provider module, model id, and the tools this turn may use. In Low-latency mode the model is the
    provider's lite one and, once the message is known, only the tools it plausibly needs are sent."""
    if settings.provider not in PROVIDERS:
        raise ValueError(f"unknown provider {settings.provider!r}")
    mod = PROVIDERS[settings.provider]["module"]
    model = mod.LITE if settings.latency_mode == latency_profile.FAST else (settings.model or mod.DEFAULT)
    if model not in mod.MODELS:
        raise ValueError(f"{model!r} is not a {settings.provider} model")
    tools = registry.select(settings.tools_enabled)
    if settings.knowledge_mode == "ragless":  # the whole knowledge base is already in the prompt
        tools = [t for t in tools if t.name != "search_knowledge"]
    if settings.latency_mode == latency_profile.FAST and message is not None:
        # Last turn's tools stay available for one more turn, so "yes, go ahead" can still check out.
        wanted = latency_profile.tools_for(message, {t.name for t in tools}) | (sticky or set())
        tools = [t for t in tools if t.name in wanted]
    return mod, model, tools


def _action(out: dict) -> str:
    args = ", ".join(f"{k}={v}" for k, v in (out.get("args") or {}).items())
    return f"{out['name']}({args}) -> {'ok' if out['ok'] else 'error: ' + str(out['result'].get('error', ''))[:80]}"


def run_turn(session_id: str, message: str, s: ChatSettings, api_key: str, cart_id: str,
             retrieve: Callable[..., dict]) -> Iterator[dict]:
    t_start = time.perf_counter()
    message = redact_keys(message)
    s = effective(s)
    fast = s.latency_mode == latency_profile.FAST
    conv = memory.get(session_id)
    mod, model, tools = resolve(s, message, sticky=conv.last_tools)
    tool_names = {t.name for t in tools}
    # Carry over only what *this* message asked for, so carried tools last exactly one extra turn.
    conv.last_tools = (latency_profile.tools_for(message, {t.name for t in registry.select(s.tools_enabled)})
                       if fast else set())

    # ---- knowledge
    user_for_llm, rag_ms = message, 0.0
    if s.knowledge_mode == "rag" and "search_knowledge" not in tool_names:
        r = retrieve(message, k=s.rag_k, rerank=s.rerank)
        rag_ms = r["timings_ms"]["total"]
        yield {"type": "rag", "auto": True, "timings_ms": r["timings_ms"],
               "hits": [{"id": h["id"], "title": h["title"]} for h in r["hits"]]}
        excerpts = "\n\n".join(f"[{i}] {h['text']}" for i, h in enumerate(r["hits"], 1))
        user_for_llm = f"{message}\n\nKnowledge base excerpts:\n{excerpts}"

    # ---- prompt
    with conv.lock:
        history = conv.history(s.verbatim_turns)
        summary = conv.context_summary(s.verbatim_turns)
    stable, volatile = prompts.build(s.persona, s.length, s.knowledge_mode, tool_names, summary, fast=fast)

    # ---- tools run against this session's cart; search uses the session's RAG settings
    ctx = registry.ToolContext(cart_id=cart_id, retrieve=lambda q, k=s.rag_k: retrieve(q, k=k, rerank=s.rerank))
    req = TurnRequest(api_key=api_key, model=model, system=stable, system_volatile=volatile, history=history,
                      user=user_for_llm, tools=tools, max_tokens=prompts.LENGTHS[s.length]["max_tokens"],
                      execute=lambda name, args: registry.call(name, args, ctx))

    text, actions, rounds, tool_ms, first_word, error = [], [], [], 0.0, None, None
    rag_tool_ms = 0.0  # retrieval done inside search_knowledge calls (part of tool_ms)
    round_ms, saw_round = 0.0, False  # wall-clock tool time per round (calls in a round may overlap)
    new_round = False  # the next text starts a new LLM round: keep a space between the two rounds' text
    for ev in mod.run(req):
        if ev["type"] == "delta":
            first_word = first_word or ms_since(t_start)
            if new_round and text and not text[-1][-1:].isspace() and not ev["text"][:1].isspace():
                ev = {**ev, "text": " " + ev["text"]}
            new_round = False
            text.append(ev["text"])
        elif ev["type"] == "tool":
            if fast and ev.get("changes_cart") and ev["ok"] and not "".join(text).strip():
                # A cart change: speak while the follow-up LLM round writes the confirmation (~1-2 s).
                first_word = first_word or ms_since(t_start)
                text.append(latency_profile.TOOL_ACK)
                yield {"type": "delta", "text": latency_profile.TOOL_ACK, "ack": True}
            tool_ms += ev["ms"]
            actions.append(_action(ev))
            if ev["name"] == "search_knowledge" and ev["ok"]:
                rag_ms += ev["result"].get("retrieval_ms", 0)
                rag_tool_ms += ev["result"].get("retrieval_ms", 0)
        elif ev["type"] == "tool_round":
            round_ms += ev["ms"]
            saw_round = True
        elif ev["type"] == "round":
            rounds.append(ev)
            new_round = True
        elif ev["type"] == "error":
            error = ev["message"]
        yield ev

    # Wall-clock per round when the adapter reports it (parallel calls overlap); else the sum of calls.
    tool_total = round_ms if saw_round else tool_ms
    reply = "".join(text).strip()
    if reply or actions:  # don't store a turn the user never got an answer to
        with conv.lock:
            conv.turns.append(Turn(user=message, assistant=reply or "(no reply)", actions=actions))
        memory.save(conv)
        memory.maybe_summarize(conv, s.verbatim_turns,
                               lambda system, prompt: mod.complete(api_key, system, prompt))

    usage = {k: sum((r["usage"].get(k) or 0) for r in rounds) for k in ("input_tokens", "output_tokens", "cached_tokens")}
    # One diagnostic line per turn: where the LLM time went, round by round. What people said is
    # logged only on a local dev server, never in production.
    log.warning("turn profile=%s model=%s/%s rounds=%s tools=%s rag=%.0fms first_word=%sms total=%.0fms tokens=%s "
                "offered=%d%s", s.latency_mode, s.provider, model,
                [(r["ttft_ms"], r["ms"]) for r in rounds], [a.split(" -> ")[0] for a in actions], rag_ms, first_word,
                ms_since(t_start), usage, len(tool_names),
                "" if PRODUCTION else f" | user={message[:120]!r} | reply={reply[:200]!r}")
    yield {
        "type": "done",
        "provider": s.provider, "model": model, "error": error, "knowledge": s.knowledge_mode,
        "profile": s.latency_mode, "tools_offered": sorted(tool_names),
        "timings_ms": {
            "rag": round(rag_ms, 1),
            "first_word": first_word,                      # message received -> first reply text
            "llm_ttft": rounds[0]["ttft_ms"] if rounds else None,
            "llm": round(sum(r["ms"] for r in rounds), 1),
            "tools": round(tool_total, 1),
            "tools_only": round(max(0.0, tool_total - rag_tool_ms), 1),  # tool time excluding retrieval
            "total": ms_since(t_start),
        },
        "rounds": len(rounds), "tool_calls": len(actions), "usage": usage,
        "memory": conv.snapshot(s.verbatim_turns),
    }
