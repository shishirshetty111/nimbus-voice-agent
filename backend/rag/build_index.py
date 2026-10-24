"""Build the FAISS index and 2D/3D UMAP projections for one embedder.

    python -m backend.rag.build_index [--embedder bge-small] [--force]

Output: rag_store/<embedder>/{index.faiss, chunks.json, coords2d.npy, coords3d.npy,
umap2d.pkl, umap3d.pkl, manifest.json}. Skips the build when chunks.jsonl is unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import time
import warnings
from pathlib import Path

import numpy as np

from backend.config import CHUNKS_PATH, SHIPPED_STORE_DIR, STORE_DIR
from backend.rag.embedder import DEFAULT_EMBEDDER, EMBEDDERS, make_embedder

# Larger neighborhoods keep the near-duplicate FAQ groups from flying off as far islands
# (grid occupancy 0.09 -> 0.23 vs n_neighbors=15, min_dist=0.1) while keeping local clusters.
UMAP_PARAMS = dict(n_neighbors=50, min_dist=0.5, metric="cosine", random_state=42)


def chunks_hash() -> str:
    return hashlib.sha256(CHUNKS_PATH.read_bytes()).hexdigest()[:16]


def store_path(embedder_key: str) -> Path:
    """The prebuilt index that ships with the code when it is current, else the writable build folder
    (the same folder locally; /tmp on Vercel)."""
    shipped = SHIPPED_STORE_DIR / embedder_key
    return shipped if _current(shipped) else STORE_DIR / embedder_key


def is_current(embedder_key: str) -> bool:
    return _current(store_path(embedder_key))


def _current(path: Path) -> bool:
    manifest = path / "manifest.json"
    if not manifest.exists():
        return False
    m = json.loads(manifest.read_text())
    return m["chunks_hash"] == chunks_hash() and m.get("umap") == UMAP_PARAMS


def build(embedder_key: str = DEFAULT_EMBEDDER, force: bool = False, embedder=None,
          key: str | None = None) -> Path:
    import faiss

    if not force and is_current(embedder_key):
        return store_path(embedder_key)
    out = STORE_DIR / embedder_key  # always writable
    out.mkdir(parents=True, exist_ok=True)

    chunks = [json.loads(line) for line in CHUNKS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    embedder = embedder or make_embedder(embedder_key)

    t0 = time.perf_counter()
    vecs = embedder.embed_docs([c["text"] for c in chunks], key=key)
    embed_s = time.perf_counter() - t0

    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)
    faiss.write_index(index, str(out / "index.faiss"))
    np.save(out / "vectors.npy", vecs)
    (out / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")

    t0 = time.perf_counter()
    import umap

    for dim in (2, 3):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # random_state disables parallelism; that's intended
            reducer = umap.UMAP(n_components=dim, **UMAP_PARAMS).fit(vecs)
        np.save(out / f"coords{dim}d.npy", reducer.embedding_.astype("float32"))
        with open(out / f"umap{dim}d.pkl", "wb") as f:
            pickle.dump(reducer, f)
    umap_s = time.perf_counter() - t0

    manifest = {
        "embedder": embedder_key,
        "model": embedder.model_name,
        "dim": int(vecs.shape[1]),
        "count": len(chunks),
        "chunks_hash": chunks_hash(),
        "index": "faiss.IndexFlatIP (exact cosine on normalized vectors)",
        "umap": UMAP_PARAMS,
        "build_seconds": {"embed": round(embed_s, 2), "umap": round(umap_s, 2)},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default=DEFAULT_EMBEDDER, choices=list(EMBEDDERS))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out = build(args.embedder, force=args.force)
    print(json.dumps(json.loads((out / "manifest.json").read_text()), indent=2))


if __name__ == "__main__":
    main()
