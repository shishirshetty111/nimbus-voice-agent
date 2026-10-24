"""RAG-less knowledge mode: the whole of knowledge/context.md goes into the system prompt."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from backend.config import CONTEXT_PATH


def count_tokens(text: str) -> int:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return len(text) // 4


@dataclass(frozen=True)
class KnowledgeContext:
    text: str
    tokens: int
    bytes: int
    sha: str
    load_ms: float

    def info(self) -> dict:
        return {"path": "knowledge/context.md", "tokens": self.tokens, "bytes": self.bytes,
                "sha": self.sha, "load_ms": self.load_ms}


_cached: KnowledgeContext | None = None


def load_context(reload: bool = False) -> KnowledgeContext:
    """Read once and keep in memory; per-turn cost is then only prompt assembly."""
    global _cached
    if _cached is None or reload:
        t0 = time.perf_counter()
        text = CONTEXT_PATH.read_text(encoding="utf-8")
        load_ms = round((time.perf_counter() - t0) * 1000, 2)
        _cached = KnowledgeContext(text=text, tokens=count_tokens(text), bytes=len(text.encode()),
                                   sha=hashlib.sha256(text.encode()).hexdigest()[:16], load_ms=load_ms)
    return _cached
