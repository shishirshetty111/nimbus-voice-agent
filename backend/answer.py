"""Answer a question from Nimbus knowledge with an OpenAI model, in RAG or RAG-less mode.

Streams events so the UI can show the reply as it arrives and measure time-to-first-token.
This is the RAG Lab's answer button; the full multi-provider LLM layer comes in M4.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from backend.context import load_context

# Current OpenAI models (developers.openai.com/api/docs/models, checked 2026-10-05).
# Reasoning effort is kept at the minimum each allows: this is a latency-sensitive voice app.
OPENAI_MODELS = {
    "gpt-6-luna": {"label": "GPT-6 Luna (lite, fastest)", "effort": "none"},
    "gpt-6.1-sol": {"label": "GPT-6.1 Sol (balanced)", "effort": "low"},
    "gpt-6-astra": {"label": "GPT-6 Astra (heavyweight)", "effort": "low"},
}
DEFAULT_MODEL = "gpt-6-luna"

RULES = """You are the Nimbus assistant. Nimbus is a business-software company.
Answer the customer's question using ONLY the knowledge provided. If the answer is not in it,
say you don't know and suggest contacting Nimbus. Be concise: 2-4 sentences, plain prose, no markdown.
Quote prices exactly as written, with their billing period."""


def build_prompt(mode: str, query: str, chunks: list[dict]) -> tuple[str, str]:
    """Return (instructions, input). RAG-less keeps the big context in the instructions so the
    prefix is identical on every call and OpenAI's automatic prompt caching can reuse it."""
    if mode == "ragless":
        return f"{RULES}\n\n# Nimbus knowledge base\n\n{load_context().text}", query
    knowledge = "\n\n".join(f"[{i}] {c['text']}" for i, c in enumerate(chunks, 1))
    return RULES, f"Knowledge:\n{knowledge}\n\nQuestion: {query}"


def stream_answer(api_key: str, model: str, mode: str, query: str, chunks: list[dict]) -> Iterator[dict]:
    from backend import clients

    t0 = time.perf_counter()
    instructions, user_input = build_prompt(mode, query, chunks)
    build_ms = round((time.perf_counter() - t0) * 1000, 2)

    client = clients.openai(api_key)
    t_req = time.perf_counter()
    ttft_ms = None
    stream = client.responses.create(
        model=model,
        instructions=instructions,
        input=user_input,
        max_output_tokens=600,  # reasoning tokens count toward this, so leave headroom
        reasoning={"effort": OPENAI_MODELS[model]["effort"]},
        prompt_cache_key=f"nimbus-{mode}",
        stream=True,
    )
    usage = None
    for event in stream:
        if event.type == "response.output_text.delta":
            if ttft_ms is None:
                ttft_ms = round((time.perf_counter() - t_req) * 1000, 1)
            yield {"type": "delta", "text": event.delta}
        elif event.type == "response.completed":
            usage = event.response.usage
        elif event.type in ("response.failed", "error"):
            err = getattr(getattr(event, "response", None), "error", None) or getattr(event, "message", "")
            yield {"type": "error", "message": f"OpenAI error: {err}"}
            return

    details = getattr(usage, "input_tokens_details", None) if usage else None
    yield {
        "type": "done",
        "mode": mode,
        "model": model,
        "timings_ms": {"prompt_build": build_ms, "ttft": ttft_ms,
                       "total": round((time.perf_counter() - t_req) * 1000, 1)},
        "usage": {
            "input_tokens": getattr(usage, "input_tokens", None),
            "cached_tokens": getattr(details, "cached_tokens", None) if details else None,
            "output_tokens": getattr(usage, "output_tokens", None),
        },
    }
