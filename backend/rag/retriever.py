"""FAISS top-k retrieval with optional cross-encoder reranking, timed per stage."""

from __future__ import annotations

import json
import pickle
import threading
import time
import warnings

import numpy as np

from backend.config import MODEL_CACHE_DIR, seed_model_cache
from backend.rag.build_index import build, store_path

DEFAULT_K = 5
DEFAULT_CANDIDATES = 10

# Registry key -> fastembed cross-encoder model
RERANKERS = {
    "minilm-l6": "Xenova/ms-marco-MiniLM-L-6-v2",
    "jina-tiny": "jinaai/jina-reranker-v1-tiny-en",
    "jina-turbo": "jinaai/jina-reranker-v1-turbo-en",
}
DEFAULT_RERANKER = "minilm-l6"


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


class Reranker:
    def __init__(self, key: str = DEFAULT_RERANKER):
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        seed_model_cache()

        self.key = key
        self.model_name = RERANKERS[key]
        self._model = TextCrossEncoder(self.model_name, cache_dir=str(MODEL_CACHE_DIR))

    def scores(self, query: str, docs: list[str]) -> list[float]:
        return [float(s) for s in self._model.rerank(query, docs)]


class Retriever:
    """One embedder's index. Thread-safe for concurrent queries."""

    def __init__(self, embedder_key: str, embedder=None, reranker: Reranker | None = None,
                 reranker_key: str = DEFAULT_RERANKER, key: str | None = None):
        import faiss

        from backend.rag.embedder import make_embedder

        self.embedder_key = embedder_key
        self.embedder = embedder or make_embedder(embedder_key)
        path = build(embedder_key, embedder=self.embedder, key=key)  # no-op when current
        self.manifest = json.loads((path / "manifest.json").read_text())
        self.index = faiss.read_index(str(path / "index.faiss"))
        self.chunks: list[dict] = json.loads((path / "chunks.json").read_text(encoding="utf-8"))
        self.coords = {d: np.load(path / f"coords{d}d.npy") for d in (2, 3)}
        self._umap_paths = {d: path / f"umap{d}d.pkl" for d in (2, 3)}
        self._umap: dict = {}
        self._rerankers: dict[str, Reranker] = {reranker.key: reranker} if reranker else {}
        self.reranker_key = reranker_key
        self._lock = threading.Lock()

    def reranker(self, key: str | None = None) -> Reranker:
        key = key or self.reranker_key
        with self._lock:
            if key not in self._rerankers:
                self._rerankers[key] = Reranker(key)
            return self._rerankers[key]

    # ------------------------------------------------------------ retrieval

    def retrieve(self, query: str, k: int = DEFAULT_K, candidates: int = DEFAULT_CANDIDATES,
                 rerank: bool = True, product_id: str | None = None,
                 category_id: str | None = None, reranker: str | None = None,
                 key: str | None = None) -> dict:
        """Embed -> FAISS search (top `candidates`) -> optional rerank -> top k."""
        t_all = time.perf_counter()
        k = max(1, min(k, len(self.chunks)))
        candidates = max(k, min(candidates, len(self.chunks))) if rerank else k
        filtered = bool(product_id or category_id)

        t0 = time.perf_counter()
        qvec = self.embedder.embed_query(query, key=key)
        embed_ms = _ms(t0)

        t0 = time.perf_counter()
        # Small corpus: with a filter, score everything and filter afterwards.
        n = len(self.chunks) if filtered else candidates
        scores, ids = self.index.search(qvec[None, :], n)
        search_ms = _ms(t0)

        pool = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0:
                continue
            c = self.chunks[idx]
            if product_id and c["product_id"] != product_id:
                continue
            if category_id and c["category_id"] != category_id:
                continue
            pool.append({"idx": int(idx), "score_vec": float(score)})
            if len(pool) == candidates:
                break
        for rank, h in enumerate(pool, 1):
            h["rank_vec"] = rank

        rerank_ms = 0.0
        if rerank and pool:
            t0 = time.perf_counter()
            rs = self.reranker(reranker).scores(query, [self.chunks[h["idx"]]["text"] for h in pool])
            rerank_ms = _ms(t0)
            for h, s in zip(pool, rs):
                h["score_rerank"] = s
            pool.sort(key=lambda h: h["score_rerank"], reverse=True)

        for rank, h in enumerate(pool, 1):
            h["rank"] = rank
            c = self.chunks[h["idx"]]
            h.update(id=c["id"], title=c["title"], type=c["type"], text=c["text"],
                     product_id=c["product_id"], category_id=c["category_id"])

        return {
            "query": query,
            "k": k,
            "candidates": candidates,
            "rerank": rerank,
            "reranker": (reranker or self.reranker_key) if rerank else None,
            "hits": pool[:k],
            "pool": pool,  # every candidate, for showing rank movement in the visualizer
            "query_vec": qvec,
            "timings_ms": {"embed": embed_ms, "search": search_ms, "rerank": rerank_ms,
                           "total": _ms(t_all)},
        }

    # ------------------------------------------------------------ projection

    def _reducer(self, dim: int):
        with self._lock:
            if dim not in self._umap:
                with open(self._umap_paths[dim], "rb") as f:
                    self._umap[dim] = pickle.load(f)
            return self._umap[dim]

    def project(self, qvec: np.ndarray, dim: int, neighbor_idx: list[int]) -> tuple[list[float], str]:
        """Place a query in the corpus' UMAP space. Falls back to the neighbors' centroid."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                xy = self._reducer(dim).transform(qvec[None, :])[0]
            return [float(v) for v in xy], "umap.transform"
        except Exception:
            pts = self.coords[dim][neighbor_idx or [0]]
            return [float(v) for v in pts.mean(axis=0)], "neighbor centroid"

    def warm(self, full: bool = True) -> None:
        """Pay model-load costs up front, not on the first user query.

        full=False (production) loads only the embedding model, about a second. The reranker and the
        UMAP projections (numba compiles them for ~20 s, holding the CPU and Python's lock, which stalls
        every voice session on the instance) are only needed by the RAG Lab, so they load on first use."""
        r = self.retrieve("warm up", k=3, candidates=5, rerank=full)
        if full:
            for d in (2, 3):
                self.project(r["query_vec"], d, [h["idx"] for h in r["hits"]])
