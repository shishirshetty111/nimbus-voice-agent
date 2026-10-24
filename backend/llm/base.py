"""What every LLM adapter receives and emits.

An adapter streams one user turn, running its own tool loop in the provider's native format.
It yields plain dicts:
    {"type": "delta", "text": ...}                      reply text as it streams
    {"type": "tool", ...registry.call() output}          after each tool executes
    {"type": "tool_round", "n", "ms", "parallel"}       after a round's tools: wall-clock time of the round
    {"type": "round", "n", "ttft_ms", "ms", "usage"}     once per LLM API call
    {"type": "error", "message": ...}                   unrecoverable problem (turn ends)
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from backend.tools.registry import Tool

MAX_TOOL_ROUNDS = 5


@dataclass
class TurnRequest:
    api_key: str
    model: str
    system: str            # stable part first (persona, rules, knowledge) ...
    system_volatile: str   # ... then per-turn parts (conversation summary), kept last for caching
    history: list[dict]    # [{"role": "user"|"assistant", "content": str}], oldest first
    user: str
    tools: list[Tool] = field(default_factory=list)
    max_tokens: int = 2000
    execute: Callable[[str, dict], dict] | None = None  # runs a tool, returns registry.call() output


_TOOL_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tools")


def execute_calls(req: "TurnRequest", calls: list[dict]) -> tuple[list[dict], float]:
    """Run one round's tool calls; returns (results in call order, wall-clock ms).

    calls: [{"name", "args"}] or [{"name", "args", "result": <ready-made error output>}] for calls that
    failed validation. Independent calls run concurrently. If any call changes the cart, the round runs
    in order instead: a cart change followed by a cart read in one round must not be reordered.
    """
    from backend.tools.registry import BY_NAME

    t0 = time.perf_counter()
    runnable = [c for c in calls if "result" not in c]
    mutating = any(BY_NAME.get(c["name"]) and BY_NAME[c["name"]].changes_cart for c in runnable)
    if len(runnable) > 1 and not mutating:
        futures = {id(c): _TOOL_POOL.submit(req.execute, c["name"], c["args"]) for c in runnable}
        outs = [c["result"] if "result" in c else futures[id(c)].result() for c in calls]
    else:
        outs = [c["result"] if "result" in c else req.execute(c["name"], c["args"]) for c in calls]
    for out in outs:
        out["parallel"] = len(runnable) > 1 and not mutating
    return outs, ms_since(t0)


def tool_round_event(outs: list[dict], wall_ms: float) -> dict:
    """Emitted once per tool round, after its tool events: wall-clock time of the round."""
    return {"type": "tool_round", "n": len(outs), "ms": wall_ms, "parallel": any(o.get("parallel") for o in outs)}


def ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def round_event(n: int, t0: float, ttft: float | None, input_tokens=None, output_tokens=None,
                cached_tokens=None) -> dict:
    return {"type": "round", "n": n, "ttft_ms": ttft, "ms": ms_since(t0),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens}}


def error_message(e: Exception) -> str:
    """Readable message from any provider SDK error."""
    body = getattr(e, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], dict):  # Gemini wraps errors in a list
        body = body[0].get("error", body[0])
    if isinstance(body, dict):
        msg = body.get("message") or (body.get("error") or {}).get("message")
        if msg:
            return msg
    return getattr(e, "message", None) or str(e)
