"""Gemini adapter (google-genai, streaming, manual function calling)."""

from __future__ import annotations

import time
from collections.abc import Iterator

from backend.llm.base import (MAX_TOOL_ROUNDS, TurnRequest, error_message, execute_calls, ms_since, round_event,
                              tool_round_event)

# ai.google.dev/gemini-api/docs/models, checked 2026-10-05. Gemini 3.x thinking cannot be turned
# off; "low" is the lowest-latency level.
MODELS = {
    "gemini-3.5-flash-lite": {"label": "Gemini 3.5 Flash-Lite (lite, fastest)", "tier": "lite"},
    "gemini-3.8-flash": {"label": "Gemini 3.8 Flash (balanced)", "tier": "balanced"},
    "gemini-3.1-pro-preview": {"label": "Gemini 3.1 Pro preview (heavyweight)", "tier": "heavy"},
}
DEFAULT = "gemini-3.8-flash"
LITE = "gemini-3.5-flash-lite"


def _client(key: str):
    from backend import clients

    return clients.gemini(key)  # reused across turns: no new TLS handshake per request


def run(req: TurnRequest) -> Iterator[dict]:
    from google.genai import types

    client = _client(req.api_key)
    contents = [types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part(text=m["content"])])
                for m in req.history]
    contents.append(types.Content(role="user", parts=[types.Part(text=req.user)]))
    config = types.GenerateContentConfig(
        system_instruction=req.system + req.system_volatile,
        max_output_tokens=req.max_tokens,
        thinking_config=types.ThinkingConfig(thinking_level="low"),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in req.tools])] if req.tools else None,
    )

    for n in range(1, MAX_TOOL_ROUNDS + 2):
        t0, ttft, parts, usage = time.perf_counter(), None, [], None
        try:
            for chunk in client.models.generate_content_stream(model=req.model, contents=contents, config=config):
                usage = chunk.usage_metadata or usage
                cand = (chunk.candidates or [None])[0]
                for part in (cand.content.parts if cand and cand.content and cand.content.parts else []):
                    parts.append(part)  # keep every part (thought signatures included) to send back
                    if part.function_call:
                        ttft = ttft or ms_since(t0)
                    elif part.text and not part.thought:
                        ttft = ttft or ms_since(t0)
                        yield {"type": "delta", "text": part.text}
        except Exception as e:
            yield {"type": "error", "message": f"Gemini: {error_message(e)}"}
            return

        yield round_event(n, t0, ttft, getattr(usage, "prompt_token_count", None),
                          getattr(usage, "candidates_token_count", None), getattr(usage, "cached_content_token_count", None))

        calls = [p.function_call for p in parts if p.function_call]
        if not calls:
            return
        if n > MAX_TOOL_ROUNDS:
            yield {"type": "error", "message": "Stopped after too many tool rounds."}
            return
        contents.append(types.Content(role="model", parts=parts))
        outs, wall = execute_calls(req, [{"name": fc.name, "args": dict(fc.args or {})} for fc in calls])
        responses = []
        for fc, out in zip(calls, outs):
            yield {"type": "tool", **out}
            responses.append(types.Part.from_function_response(name=fc.name, response={"result": out["result"]}))
        yield tool_round_event(outs, wall)
        contents.append(types.Content(role="user", parts=responses))


def complete(api_key: str, system: str, prompt: str, max_tokens: int = 1000) -> str:
    """One-shot text completion on the lite model (used for memory summaries)."""
    from google.genai import types

    client = _client(api_key)  # keep a reference: the SDK closes a collected client mid-request
    r = client.models.generate_content(
        model=LITE, contents=prompt,
        config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens,
                                           thinking_config=types.ThinkingConfig(thinking_level="low")))
    return (r.text or "").strip()
