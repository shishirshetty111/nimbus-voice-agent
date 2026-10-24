"""OpenAI adapter (Responses API, streaming, function calling)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from backend.llm.base import (MAX_TOOL_ROUNDS, TurnRequest, error_message, execute_calls, ms_since, round_event,
                              tool_round_event)
from backend.tools.registry import to_openai

# developers.openai.com/api/docs/models, checked 2026-10-05. Lowest reasoning effort each allows,
# because this is a voice agent and reasoning time is user-visible latency.
MODELS = {
    "gpt-6-luna": {"label": "GPT-6 Luna (lite, fastest)", "tier": "lite", "effort": "none"},
    "gpt-6.1-sol": {"label": "GPT-6.1 Sol (balanced)", "tier": "balanced", "effort": "low"},
    "gpt-6-astra": {"label": "GPT-6 Astra (heavyweight)", "tier": "heavy", "effort": "low"},
}
DEFAULT = "gpt-6-luna"
LITE = "gpt-6-luna"


def _client(key: str):
    from backend import clients

    return clients.openai(key)  # reused across turns: no new TLS handshake per request


def run(req: TurnRequest) -> Iterator[dict]:
    client = _client(req.api_key)
    items: list = [{"role": m["role"], "content": m["content"]} for m in req.history]
    items.append({"role": "user", "content": req.user})
    tools = to_openai(req.tools)

    for n in range(1, MAX_TOOL_ROUNDS + 2):
        t0, ttft, final = time.perf_counter(), None, None
        said, muted = False, False  # a finished message item / skipping a restatement of it
        try:
            stream = client.responses.create(
                model=req.model,
                instructions=req.system + req.system_volatile,
                input=items,
                max_output_tokens=req.max_tokens,
                reasoning={"effort": MODELS[req.model]["effort"]},
                prompt_cache_key="nimbus-agent",
                stream=True,
                **({"tools": tools, "parallel_tool_calls": True} if tools else {}),
            )
            for ev in stream:
                if ev.type == "response.output_text.delta":
                    ttft = ttft or ms_since(t0)
                    if not muted:
                        yield {"type": "delta", "text": ev.delta}
                elif ev.type == "response.output_item.added" and ev.item.type == "function_call":
                    ttft = ttft or ms_since(t0)
                    said = muted = False
                elif ev.type == "response.output_item.added" and ev.item.type == "message":
                    # The lite model sometimes writes a second message that restates the first one
                    # ("Which product did you mean? Which product do you mean by..."): speak only one.
                    muted = said
                elif ev.type == "response.output_item.done" and ev.item.type == "message":
                    said = True
                elif ev.type in ("response.completed", "response.incomplete"):
                    final = ev.response
                elif ev.type in ("response.failed", "error"):
                    err = getattr(getattr(ev, "response", None), "error", None) or getattr(ev, "message", "")
                    yield {"type": "error", "message": f"OpenAI: {getattr(err, 'message', err)}"}
                    return
        except Exception as e:
            yield {"type": "error", "message": f"OpenAI: {error_message(e)}"}
            return

        usage = final.usage if final else None
        details = getattr(usage, "input_tokens_details", None)
        yield round_event(n, t0, ttft, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
                          getattr(details, "cached_tokens", None))

        calls = [it for it in (final.output if final else []) if it.type == "function_call"]
        if not calls:
            return
        if n > MAX_TOOL_ROUNDS:
            yield {"type": "error", "message": "Stopped after too many tool rounds."}
            return
        # Feed the whole output back (reasoning items included), then one result per call.
        items += [it.model_dump(exclude_none=True) for it in final.output]
        specs = []
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            specs.append({"name": c.name, "args": args} if isinstance(args, dict) else {
                "name": c.name, "args": c.arguments, "result": {
                    "name": c.name, "args": c.arguments, "ok": False, "ms": 0, "changes_cart": False,
                    "result": {"error": "Arguments were not valid JSON."}}})
        outs, wall = execute_calls(req, specs)  # concurrently when independent
        for c, out in zip(calls, outs):
            yield {"type": "tool", **out}
            items.append({"type": "function_call_output", "call_id": c.call_id, "output": json.dumps(out["result"])})
        yield tool_round_event(outs, wall)


def complete(api_key: str, system: str, prompt: str, max_tokens: int = 600) -> str:
    """One-shot text completion on the lite model (used for memory summaries)."""
    r = _client(api_key).responses.create(model=LITE, instructions=system, input=prompt,
                                          max_output_tokens=max_tokens, reasoning={"effort": "none"})
    return r.output_text.strip()
