"""Retrieval quality on tests/rag_eval.jsonl: recall@1/3/5, MRR and latency.

    python -m backend.rag.eval [--embedder bge-small] [--candidates 10 20] [--rerankers minilm-l6 jina-tiny]

A question counts as a hit at k when any of its relevant chunk ids (fnmatch globs allowed,
e.g. "*:faq:3") is in the top k. Compares plain vector search with each reranker x candidate count.
"""

from __future__ import annotations

import argparse
import json
import statistics
from fnmatch import fnmatch

from backend.config import ROOT
from backend.rag.embedder import DEFAULT_EMBEDDER, EMBEDDERS
from backend.rag.retriever import DEFAULT_RERANKER, RERANKERS, Retriever

EVAL_PATH = ROOT / "tests" / "rag_eval.jsonl"


def load_eval() -> list[dict]:
    return [json.loads(line) for line in EVAL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_relevant(chunk_id: str, patterns: list[str]) -> bool:
    return any(fnmatch(chunk_id, p) for p in patterns)


def evaluate(retriever: Retriever, rerank: bool, candidates: int = 20, reranker: str | None = None,
             k: int = 5) -> dict:
    rows, ranks, totals, rerank_ms = [], [], [], []
    for item in load_eval():
        r = retriever.retrieve(item["q"], k=k, candidates=candidates, rerank=rerank, reranker=reranker)
        ids = [h["id"] for h in r["hits"]]
        rank = next((i for i, cid in enumerate(ids, 1) if is_relevant(cid, item["relevant"])), None)
        ranks.append(rank)
        totals.append(r["timings_ms"]["total"])
        rerank_ms.append(r["timings_ms"]["rerank"])
        rows.append({"q": item["q"], "rank": rank, "top1": ids[0]})
    n = len(ranks)
    return {
        "recall@1": sum(1 for r in ranks if r and r <= 1) / n,
        "recall@3": sum(1 for r in ranks if r and r <= 3) / n,
        "recall@5": sum(1 for r in ranks if r and r <= 5) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
        "total_ms_p50": statistics.median(totals),
        "rerank_ms_p50": statistics.median(rerank_ms),
        "misses@5": [row for row in rows if not row["rank"]],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedder", default=DEFAULT_EMBEDDER, choices=list(EMBEDDERS))
    ap.add_argument("--candidates", type=int, nargs="+", default=[10])
    ap.add_argument("--rerankers", nargs="+", default=[DEFAULT_RERANKER], choices=list(RERANKERS))
    ap.add_argument("--show-misses", action="store_true")
    args = ap.parse_args()

    retriever = Retriever(args.embedder)
    retriever.warm()
    for key in args.rerankers:
        retriever.reranker(key).scores("warm up", ["warm up"])
    print(f"embedder={args.embedder}  questions={len(load_eval())}\n")
    print(f"{'mode':<24}{'R@1':>6}{'R@3':>6}{'R@5':>6}{'MRR':>6}{'rerank p50':>12}{'total p50':>11}")

    runs = [("vector only", False, 5, None)]
    runs += [(f"{key} / {c} cand", True, c, key) for key in args.rerankers for c in args.candidates]
    for label, rerank, cand, key in runs:
        m = evaluate(retriever, rerank, cand, key)
        print(f"{label:<24}{m['recall@1']:>6.2f}{m['recall@3']:>6.2f}{m['recall@5']:>6.2f}{m['mrr']:>6.2f}"
              f"{m['rerank_ms_p50']:>10.0f}ms{m['total_ms_p50']:>9.0f}ms")
        if args.show_misses:
            for row in m["misses@5"]:
                print(f"    miss: {row['q']!r} -> top1 {row['top1']}")


if __name__ == "__main__":
    main()
