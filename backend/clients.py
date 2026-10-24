"""Shared, long-lived provider clients (one per provider and API key).

Creating an SDK client per request means a new TCP + TLS connection every turn or sentence, often
100-300 ms. Reusing one client keeps connections alive. The SDK clients are thread-safe. Keeping a
reference also stops the Gemini SDK closing a client that is still streaming when it is collected.
"""

from __future__ import annotations

import threading
from functools import lru_cache

import httpx


@lru_cache(maxsize=64)
def openai(key: str):
    from openai import OpenAI

    return OpenAI(api_key=key)


@lru_cache(maxsize=64)
def anthropic(key: str):
    import anthropic as sdk

    return sdk.Anthropic(api_key=key)


@lru_cache(maxsize=64)
def gemini(key: str):
    from google import genai

    return genai.Client(api_key=key)


_http: httpx.Client | None = None
_http_lock = threading.Lock()


def http() -> httpx.Client:
    """One pooled HTTP client for plain REST providers (ElevenLabs)."""
    global _http
    with _http_lock:
        if _http is None:
            _http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0),
                                 limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120))
        return _http
