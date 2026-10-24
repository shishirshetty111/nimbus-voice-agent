"""Chat pipeline tests with a fake LLM provider (no network, no API keys)."""

import time
import types

import pytest

import backend.tools.registry as reg
from backend import chat, prompts
from backend.llm import PROVIDERS
from backend.llm.anthropic_llm import _valid_input
from backend.memory import CANCELLED, Memory, Turn
from backend.tools.cart import CartStore


class FakeLLM:
    """Says one sentence; if the user mentions 'add', calls add_to_cart first."""
    MODELS = {"fake-1": {"label": "Fake", "tier": "lite"}}
    DEFAULT = LITE = "fake-1"

    def __init__(self):
        self.requests = []

    def run(self, req):
        self.requests.append(req)
        if "add" in req.user:
            out = req.execute("add_to_cart", {"product": "crm"})
            yield {"type": "tool", **out}
            yield {"type": "round", "n": 1, "ttft_ms": 5.0, "ms": 10.0,
                   "usage": {"input_tokens": 100, "output_tokens": 10, "cached_tokens": 0}}
        yield {"type": "delta", "text": "Done. "}
        yield {"type": "delta", "text": f"You said: {req.user[:20]}"}
        yield {"type": "round", "n": 2, "ttft_ms": 4.0, "ms": 9.0,
               "usage": {"input_tokens": 120, "output_tokens": 12, "cached_tokens": 50}}

    def complete(self, api_key, system, prompt):
        return "SUMMARY: " + prompt.count("User:").__str__() + " turns merged"


@pytest.fixture
def fake(monkeypatch, tmp_path):
    llm = FakeLLM()
    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": llm})
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "carts.json"))
    monkeypatch.setattr(chat, "memory", Memory())
    return llm


def retrieve_stub(q, k=5, rerank=False):
    return {"hits": [{"id": "policy:refund", "title": "Refunds", "text": "Refunds within 30 days."}][:k],
            "timings_ms": {"total": 3.0}}


def turn(msg, settings, session="sess-abc-123"):
    return list(chat.run_turn(session, msg, settings, "key", "cart-test-1", retrieve_stub))


def test_turn_streams_tools_and_reports_timings(fake):
    evs = turn("please add crm", chat.ChatSettings(provider="fake"))
    kinds = [e["type"] for e in evs]
    assert kinds.count("delta") == 2 and "tool" in kinds and kinds[-1] == "done"
    done = evs[-1]
    assert done["tool_calls"] == 1 and done["rounds"] == 2
    assert done["usage"] == {"input_tokens": 220, "output_tokens": 22, "cached_tokens": 50}
    assert done["timings_ms"]["first_word"] is not None and done["timings_ms"]["total"] >= done["timings_ms"]["first_word"]
    assert reg.store.view("cart-test-1")["totals"]["items"] == 1  # the tool really ran


def test_tool_selection_reaches_llm(fake):
    turn("hi", chat.ChatSettings(provider="fake", tools_enabled=["add_to_cart", "checkout"]))
    assert [t.name for t in fake.requests[-1].tools] == ["add_to_cart", "checkout"]
    turn("hi", chat.ChatSettings(provider="fake", tools_enabled=[]))
    assert fake.requests[-1].tools == [] and "You have no tools" in fake.requests[-1].system


def test_auto_rag_when_search_tool_disabled(fake):
    evs = turn("refunds?", chat.ChatSettings(provider="fake", tools_enabled=["add_to_cart"]))
    assert evs[0]["type"] == "rag" and evs[0]["auto"]
    assert "Knowledge base excerpts" in fake.requests[-1].user
    assert evs[-1]["timings_ms"]["rag"] == 3.0


def test_ragless_puts_context_in_prompt_and_drops_search(fake):
    turn("refunds?", chat.ChatSettings(provider="fake", knowledge_mode="ragless"))
    req = fake.requests[-1]
    assert "search_knowledge" not in {t.name for t in req.tools}
    assert "# Nimbus knowledge base" in req.system and len(req.system) > 50_000


def test_memory_window_and_summary(fake):
    s = chat.ChatSettings(provider="fake", verbatim_turns=2)
    for i in range(5):
        turn(f"message number {i}", s)
    deadline = time.time() + 5
    conv = chat.memory.get("sess-abc-123")
    while conv.summarizing and time.time() < deadline:
        time.sleep(0.05)
    turn("message number 5", s)
    req = fake.requests[-1]
    assert [m["content"] for m in req.history if m["role"] == "user"] == ["message number 3", "message number 4"]
    assert "Earlier in this conversation" in req.system_volatile  # older turns reach the model as a summary
    assert conv.summarized >= 1 and conv.summary.startswith("SUMMARY")


def test_actions_and_cancellation_recorded_in_history():
    t = Turn(user="add crm", assistant="Adding the CRM now", actions=["add_to_cart(product=crm) -> ok"], cancelled=True)
    text = t.assistant_text()
    assert text.startswith("Adding the CRM now " + CANCELLED) and "add_to_cart" in text


def test_prompt_variants():
    stable, volatile = prompts.build("", "low", "rag", {"search_knowledge", "add_to_cart"}, summary="")
    assert "search_knowledge tool" in stable and "one or two short sentences" in stable and volatile == ""
    assert prompts.DEFAULT_PERSONA in stable
    custom, _ = prompts.build("You are Max.", "high", "rag", set())
    assert custom.startswith("You are Max.") and "Knowledge base excerpts" in custom


def test_unknown_provider_or_model():
    with pytest.raises(ValueError):
        chat.resolve(chat.ChatSettings(provider="nope"))
    with pytest.raises(ValueError):
        chat.resolve(chat.ChatSettings(provider="openai", model="gpt-2"))


def test_anthropic_input_validation():
    add = reg.BY_NAME["add_to_cart"]
    assert _valid_input(add, {"product": "crm", "seats": 2})
    assert not _valid_input(add, {"seats": 2})                   # missing required
    assert not _valid_input(add, {"product": "crm", "seats": "two"})  # wrong type
    assert not _valid_input(add, {"product": "crm", "colour": "red"})  # unknown field
    assert not _valid_input(add, "crm")
