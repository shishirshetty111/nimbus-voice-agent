"""Text embedders. All return L2-normalized float32 vectors so inner product == cosine."""

from __future__ import annotations

import numpy as np

from backend.config import MODEL_CACHE_DIR, api_key, seed_model_cache

# bge v1.5 retrieves better when short queries carry this instruction (documents do not).
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype="float32")
    return v / np.clip(np.linalg.norm(v, axis=-1, keepdims=True), 1e-12, None)


class FastEmbedEmbedder:
    """Local ONNX model via fastembed. No network after the first model download."""

    needs_key = None

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding

        seed_model_cache()
        self.model_name = model
        self._model = TextEmbedding(model, cache_dir=str(MODEL_CACHE_DIR))
        self.dim = len(next(iter(self._model.embed(["dim probe"]))))

    def embed_docs(self, texts: list[str], key: str | None = None) -> np.ndarray:
        return _normalize(np.stack(list(self._model.embed(texts, batch_size=64))))

    def embed_query(self, text: str, key: str | None = None) -> np.ndarray:
        prefix = BGE_QUERY_PREFIX if "bge" in self.model_name.lower() else ""
        return _normalize(next(iter(self._model.embed([prefix + text]))))


class MissingKeyError(RuntimeError):
    pass


class OpenAIEmbedder:
    """OpenAI embeddings API. Adds a network round-trip per query.

    The key comes per call (from the browser's API-keys panel) or falls back to the server .env.
    """

    needs_key = "openai"

    def __init__(self, model: str = "text-embedding-3-small"):
        self.model_name = model
        self.dim = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}.get(model, 1536)
        self._clients: dict[str, object] = {}

    def _client(self, key: str | None):
        from openai import OpenAI

        key = key or api_key("openai")
        if not key:
            raise MissingKeyError("No OpenAI API key: add one in the API keys panel or set OPENAI_API_KEY")
        if key not in self._clients:
            self._clients[key] = OpenAI(api_key=key)
        return self._clients[key]

    def embed_docs(self, texts: list[str], key: str | None = None) -> np.ndarray:
        client, out = self._client(key), []
        for i in range(0, len(texts), 256):
            resp = client.embeddings.create(model=self.model_name, input=texts[i:i + 256])
            out += [d.embedding for d in resp.data]
        return _normalize(np.array(out))

    def embed_query(self, text: str, key: str | None = None) -> np.ndarray:
        resp = self._client(key).embeddings.create(model=self.model_name, input=[text])
        return _normalize(np.array(resp.data[0].embedding))


# Registry key -> (label shown in UI, factory)
EMBEDDERS = {
    "bge-small": ("BAAI/bge-small-en-v1.5 (local, 384-d)", lambda: FastEmbedEmbedder("BAAI/bge-small-en-v1.5")),
    "openai-small": ("OpenAI text-embedding-3-small (API, 1536-d)", lambda: OpenAIEmbedder("text-embedding-3-small")),
}
DEFAULT_EMBEDDER = "bge-small"


def make_embedder(key: str):
    if key not in EMBEDDERS:
        raise KeyError(f"unknown embedder {key!r}; choose from {list(EMBEDDERS)}")
    return EMBEDDERS[key][1]()
