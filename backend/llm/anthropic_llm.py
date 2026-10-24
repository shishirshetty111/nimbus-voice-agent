"""Anthropic adapter (Messages API, streaming manual tool loop)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from backend.llm.base import (MAX_TOOL_ROUNDS, TurnRequest, error_message, execute_calls, ms_since, round_event,
                              tool_round_event)
from backend.tools.registry import to_anthropic

# Thinking is always on for Opus 5.5 / Sonnet 5.5, so latency is steered with effort="low".
# Haiku 4.5 takes neither thinking nor effort here. Opus 5.5 / Sonnet 5.5 also get the
# server-side refusal fallback ("default" routing), as Anthropic recommends for these models.
MODELS = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5 (most capable)", "tier": "heavy", "effort": "low", "fallback": True},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5 (balanced)", "tier": "balanced", "effort": "low", "fallback": True},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5 (lite, fastest)", "tier": "lite", "effort": None, "fallback": False},
}
DEFAULT = "claude-opus-5-5"
LITE = "claude-haiku-4-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _client(key: str):
    from backend import clients

    return clients.anthropic(key)  # reused across turns: no new TLS handshake per request


def _stream(client, model: str, **kw):
    spec = MODELS[model]
    if spec["effort"]:
        kw["output_config"] = {"effort": spec["effort"]}
    if spec["fallback"]:
        return client.beta.messages.stream(model=model, betas=[FALLBACK_BETA], fallbacks="default", **kw)
    return client.messages.stream(model=model, **kw)


def _valid_input(tool, args) -> bool:
    """Eager input streaming skips server-side validation, so check required fields and types here."""
    if not isinstance(args, dict):
        return False
    props = tool.parameters.get("properties", {})
    if any(r not in args for r in tool.parameters.get("required", [])):
        return False
    types = {"string": str, "integer": int}
    return all(k in props and isinstance(v, types.get(props[k].get("type"), object)) and not isinstance(v, bool)
               for k, v in args.items())


def run(req: TurnRequest) -> Iterator[dict]:
    client = _client(req.api_key)
    by_name = {t.name: t for t in req.tools}
    tools = [{**t, "eager_input_streaming": True} for t in to_anthropic(req.tools)]
    # Stable system text gets the cache breakpoint; the per-turn summary goes after it.
    system = [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
    if req.system_volatile.strip():
        system.append({"type": "text", "text": req.system_volatile})
    messages = [{"role": m["role"], "content": m["content"]} for m in req.history]
    messages.append({"role": "user", "content": req.user})

    n, json_retries = 0, 0
    while True:
        n += 1
        t0, ttft = time.perf_counter(), None
        try:
            with _stream(client, req.model, max_tokens=req.max_tokens, system=system, messages=messages,
                         **({"tools": tools} if tools else {})) as stream:
                for ev in stream:
                    if ev.type == "text":
                        ttft = ttft or ms_since(t0)
                        yield {"type": "delta", "text": ev.text}
                    elif ev.type == "content_block_start" and ev.content_block.type == "tool_use":
                        ttft = ttft or ms_since(t0)
                response = stream.get_final_message()
            json_retries = 0
        except ValueError:
            # Tool input JSON the SDK could not parse at all; there is no tool_use id to answer,
            # so re-issue the same request (bounded).
            json_retries += 1
            if json_retries > 2:
                yield {"type": "error", "message": "Claude returned unparseable tool input."}
                return
            continue
        except Exception as e:
            yield {"type": "error", "message": f"Anthropic: {error_message(e)}"}
            return

        u = response.usage
        yield round_event(n, t0, ttft, u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", None))

        if response.stop_reason == "refusal":
            yield {"type": "error", "message": "Claude declined to answer this request."}
            return
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return
        if response.stop_reason == "max_tokens":
            yield {"type": "error", "message": "A tool call was cut off by the token limit."}
            return
        if n > MAX_TOOL_ROUNDS:
            yield {"type": "error", "message": "Stopped after too many tool rounds."}
            return

        specs = []
        for b in tool_uses:
            tool = by_name.get(b.name)
            specs.append({"name": b.name, "args": b.input} if tool and _valid_input(tool, b.input) else {
                "name": b.name, "args": b.input, "result": {
                    "name": b.name, "args": b.input, "ok": False, "ms": 0, "changes_cart": False,
                    "result": {"error": "INVALID_INPUT: arguments missing or of the wrong type", "input": b.input}}})
        outs, wall = execute_calls(req, specs)  # concurrently when independent
        results = []
        for b, out in zip(tool_uses, outs):
            yield {"type": "tool", **out}
            # Every result goes back in ONE user message (splitting them discourages parallel calls).
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out["result"]),
                            **({"is_error": True} if not out["ok"] else {})})
        yield tool_round_event(outs, wall)
        # Append the full content unchanged (thinking and fallback blocks must be echoed back).
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": results})


def complete(api_key: str, system: str, prompt: str, max_tokens: int = 1000) -> str:
    """One-shot text completion on the lite model (used for memory summaries)."""
    r = _client(api_key).messages.create(model=LITE, max_tokens=max_tokens, system=system,
                                         messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in r.content if b.type == "text").strip()
