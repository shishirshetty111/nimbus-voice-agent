"""Retriever tests against the real bge-small index (builds it on first run, ~1 min)."""

import pytest

from backend.context import load_context
from backend.rag.retriever import Retriever


@pytest.fixture(scope="module")
def retriever():
    return Retriever("bge-small")


def test_refund_policy_in_top3(retriever):
    r = retriever.retrieve("how do refunds work", k=3, candidates=10, rerank=True)
    assert "policy:refund" in [h["id"] for h in r["hits"]]


def test_hits_are_ranked_and_bounded(retriever):
    r = retriever.retrieve("what does Nimbus CRM cost", k=4, candidates=10, rerank=True)
    assert [h["rank"] for h in r["hits"]] == [1, 2, 3, 4]
    assert len(r["pool"]) == 10
    scores = [h["score_rerank"] for h in r["hits"]]
    assert scores == sorted(scores, reverse=True)


def test_without_rerank_orders_by_cosine(retriever):
    r = retriever.retrieve("password manager", k=5, rerank=False)
    scores = [h["score_vec"] for h in r["hits"]]
    assert scores == sorted(scores, reverse=True)
    assert r["timings_ms"]["rerank"] == 0


def test_product_filter(retriever):
    r = retriever.retrieve("pricing", k=5, rerank=False, product_id="nimbus-vault")
    assert r["hits"] and all(h["product_id"] == "nimbus-vault" for h in r["hits"])


def test_timings_reported(retriever):
    t = retriever.retrieve("free trial", k=3)["timings_ms"]
    assert set(t) == {"embed", "search", "rerank", "total"}
    assert t["total"] >= t["embed"] + t["search"]


def test_query_projection_matches_dim(retriever):
    r = retriever.retrieve("kanban boards", k=3, rerank=False)
    for dim in (2, 3):
        xyz, _ = retriever.project(r["query_vec"], dim, [h["idx"] for h in r["hits"]])
        assert len(xyz) == dim


def test_ragless_context_loaded():
    ctx = load_context()
    assert ctx.tokens > 20000 and "Nimbus CRM" in ctx.text
