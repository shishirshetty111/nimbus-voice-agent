"""Low-latency mode: profile overrides, tool routing, parallel TTS ordering, quick ASR finals."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

import backend.tools.registry as reg
from backend import chat, latency_profile, prompts
from backend.llm import PROVIDERS
from backend.memory import Memory
from backend.tools.cart import CartStore
from backend.voice import asr as asr_mod
from backend.voice import session as vsession


# ------------------------------------------------------------------ profile + routing

def test_profile_overrides_and_lite_model():
    s = chat.effective(chat.ChatSettings(latency_mode="fast", provider="openai", model="gpt-6-astra",
                                         length="high", knowledge_mode="ragless", rag_k=8, rerank=True))
    assert (s.length, s.verbatim_turns, s.knowledge_mode, s.rag_k, s.rerank) == ("low", 4, "rag", 3, False)
    _, model, _ = chat.resolve(s, "hi")
    assert model == "gpt-6-luna"
    normal = chat.ChatSettings(provider="openai", model="gpt-6-astra")
    assert chat.effective(normal) is normal and chat.resolve(normal)[1] == "gpt-6-astra"


@pytest.mark.parametrize("message,expected", [
    ("what is your refund policy", set()),
    ("add crm pro for 3 users", {"add_to_cart", "get_cart_total", "checkout"}),
    ("how much would I save paying annually", {"annual_savings", "annual_price"}),
    ("which product is the cheapest", {"sort_products_by_price", "top_k_expensive"}),
])
def test_tool_routing(message, expected):
    s = chat.effective(chat.ChatSettings(latency_mode="fast"))
    names = {t.name for t in chat.resolve(s, message)[2]}
    assert expected <= names and "search_knowledge" not in names
    if not expected:
        assert names == set()


def test_routing_respects_disabled_tools():
    s = chat.effective(chat.ChatSettings(latency_mode="fast", tools_enabled=["get_cart_total"]))
    assert {t.name for t in chat.resolve(s, "add crm to my cart")[2]} == {"get_cart_total"}


def test_fast_prompt_rules():
    fast, _ = prompts.build("", "low", "rag", {"add_to_cart"}, fast=True)
    assert "directly answers" in fast and "just call the tool" in fast
    quiet, _ = prompts.build("", "low", "rag", set(), fast=True)
    assert "You have no tools" not in quiet  # no tools this turn is routing, not a limitation
    normal, _ = prompts.build("", "low", "rag", set())
    assert "You have no tools" in normal and "directly answers" not in normal


class RecordingLLM:
    MODELS = {"fake-1": {"label": "Fake"}, "fake-lite": {"label": "Fake lite"}}
    DEFAULT, LITE = "fake-1", "fake-lite"

    def __init__(self):
        self.requests = []

    def run(self, req):
        self.requests.append(req)
        yield {"type": "delta", "text": "Okay."}

    def complete(self, *a):
        return "summary"


@pytest.fixture
def llm(monkeypatch, tmp_path):
    fake = RecordingLLM()
    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": fake})
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "carts.json"))
    monkeypatch.setattr(chat, "memory", Memory())
    return fake


def retrieve_stub(q, k=5, rerank=False):
    return {"hits": [{"id": f"c{i}", "title": "t", "text": "x"} for i in range(k)], "timings_ms": {"total": 2.0}}


def test_fast_turn_auto_retrieves_and_keeps_tools_one_turn(llm):
    s = chat.ChatSettings(provider="fake", latency_mode="fast")
    evs = list(chat.run_turn("sess-fast-1", "add crm to my cart", s, "k", "cart-fast-1", retrieve_stub))
    assert evs[0]["type"] == "rag" and len(evs[0]["hits"]) == 3       # auto retrieval, k=3
    assert llm.requests[-1].model == "fake-lite"
    assert "add_to_cart" in {t.name for t in llm.requests[-1].tools}
    assert evs[-1]["profile"] == "fast"
    list(chat.run_turn("sess-fast-1", "yes go ahead", s, "k", "cart-fast-1", retrieve_stub))
    assert "checkout" in {t.name for t in llm.requests[-1].tools}       # sticky from the previous turn
    list(chat.run_turn("sess-fast-1", "what is the refund policy", s, "k", "cart-fast-1", retrieve_stub))
    list(chat.run_turn("sess-fast-1", "thanks", s, "k", "cart-fast-1", retrieve_stub))
    assert llm.requests[-1].tools == []                                  # ...but only for one turn


# ------------------------------------------------------------------ parallel TTS keeps order

def test_parallel_tts_emits_sentences_in_order(monkeypatch, llm, tmp_path):
    class TwoSentences(RecordingLLM):
        def run(self, req):
            for w in "Sentence one is slow to synthesize. Sentence two is quick.".split(" "):
                yield {"type": "delta", "text": w + " "}
    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": TwoSentences()})
    started = []

    def tts_stream(provider, text, voice, key, fast=False):
        started.append((text[:12], time.perf_counter()))
        time.sleep(0.4 if "one" in text else 0.05)       # the first sentence finishes last
        yield (b"\x01\x00" if "one" in text else b"\x02\x00") * 2400
    monkeypatch.setattr(vsession.tts, "stream", tts_stream)
    monkeypatch.setattr(vsession, "api_key", lambda p: "k")
    from backend.app import app

    with TestClient(app).websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "start", "cart_id": "voice-fast-cart", "session_id": "voice-fast-sess",
                                 "settings": {"mode": "streaming", "asr_provider": "browser", "tts_provider": "openai",
                                              "chat": {"provider": "fake", "latency_mode": "fast"}}}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        frames, done = [], None
        while done is None:
            m = ws.receive()
            if m.get("bytes") is not None:
                frames.append(m["bytes"][4:6])
            elif json.loads(m["text"])["type"] == "turn_done":
                done = json.loads(m["text"])
    assert frames == [b"\x01\x00", b"\x02\x00"]                  # sentence one's audio first, despite finishing last
    assert abs(started[1][1] - started[0][1]) < 0.3              # ...because both synthesized at the same time
    assert done["profile"] == "fast"


# ------------------------------------------------------------------ quick ASR final

class SlowFinal(asr_mod.StreamingASR):
    def __init__(self, partial):
        super().__init__("k", lambda t: None)
        self.partial = partial

    def latest(self):
        return self.partial


def test_quick_final_falls_back_to_partial():
    async def run(partial, quick):
        a = SlowFinal(partial)
        fut = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(0.5, fut.set_result, "the final transcript")
        t = time.perf_counter()
        text = await a.await_final(fut, quick)
        return text, a.used_partial, time.perf_counter() - t

    text, used, took = asyncio.run(run("the partial", 0.1))
    assert (text, used) == ("the partial", True) and took < 0.3
    text, used, _ = asyncio.run(run("", 0.1))          # no partial yet: wait for the real final
    assert (text, used) == ("the final transcript", False)
    text, used, _ = asyncio.run(run("the partial", None))  # normal mode: always the final
    assert (text, used) == ("the final transcript", False)


# ------------------------------------------------------------------ spoken-text fixes (from live logs)

def _fake_provider(monkeypatch, tmp_path, events):
    import backend.tools.registry as reg
    from backend import chat
    from backend.llm import PROVIDERS
    from backend.memory import Memory
    from backend.tools.cart import CartStore

    class Fake:
        MODELS = {"fake-1": {"label": "Fake"}}
        DEFAULT = LITE = "fake-1"

        def run(self, r):
            yield from events

        def complete(self, *a):
            return ""

    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": Fake()})
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "c.json"))
    monkeypatch.setattr(chat, "memory", Memory())
    return chat


def _round(n):
    return {"type": "round", "n": n, "ttft_ms": 1.0, "ms": 2.0, "usage": {}}


def _tool():
    return {"type": "tool", "name": "add_to_cart", "args": {}, "ok": True, "ms": 1.0, "changes_cart": True, "result": {}}


def test_text_from_two_rounds_keeps_a_space(monkeypatch, tmp_path):
    chat = _fake_provider(monkeypatch, tmp_path, [
        {"type": "delta", "text": "Adding that now."}, _round(1), _tool(), {"type": "delta", "text": "Added two seats."}, _round(2)])
    evs = list(chat.run_turn("s-sp", "add 2 crm", chat.ChatSettings(provider="fake"), "k", "c-sp",
                             lambda q, k=5, rerank=False: {"hits": [], "timings_ms": {"total": 0}}))
    assert "".join(e["text"] for e in evs if e["type"] == "delta") == "Adding that now. Added two seats."


def test_fast_mode_acknowledges_a_silent_tool_round(monkeypatch, tmp_path):
    from backend import latency_profile
    chat = _fake_provider(monkeypatch, tmp_path, [_round(1), _tool(), {"type": "delta", "text": "Added two seats."}, _round(2)])
    evs = list(chat.run_turn("s-ack", "add 2 crm to the cart", chat.ChatSettings(provider="fake", latency_mode="fast"),
                             "k", "c-ack", lambda q, k=5, rerank=False: {"hits": [], "timings_ms": {"total": 0}}))
    deltas = [e for e in evs if e["type"] == "delta"]
    assert deltas[0]["ack"] and deltas[0]["text"] == latency_profile.TOOL_ACK
    assert "".join(e["text"] for e in deltas) == "Okay. Added two seats."


def test_normal_mode_has_no_ack(monkeypatch, tmp_path):
    chat = _fake_provider(monkeypatch, tmp_path, [_round(1), _tool(), {"type": "delta", "text": "Added."}, _round(2)])
    evs = list(chat.run_turn("s-noack", "add crm", chat.ChatSettings(provider="fake"), "k", "c-noack",
                             lambda q, k=5, rerank=False: {"hits": [], "timings_ms": {"total": 0}}))
    assert [e["text"] for e in evs if e["type"] == "delta"] == ["Added."]


def test_openai_speaks_only_one_message_per_round(monkeypatch):
    from types import SimpleNamespace as NS
    from backend.llm import openai_llm
    from backend.llm.base import TurnRequest

    msg = NS(type="message")
    usage = NS(input_tokens=1, output_tokens=1, input_tokens_details=NS(cached_tokens=0))
    stream = [NS(type="response.output_item.added", item=msg), NS(type="response.output_text.delta", delta="Which product?"),
              NS(type="response.output_item.done", item=msg),
              NS(type="response.output_item.added", item=msg), NS(type="response.output_text.delta", delta="Which product do you mean?"),
              NS(type="response.output_item.done", item=msg),
              NS(type="response.completed", response=NS(output=[], usage=usage))]
    monkeypatch.setattr(openai_llm, "_client", lambda key: NS(responses=NS(create=lambda **kw: iter(stream))))
    r = TurnRequest(api_key="k", model="gpt-6-luna", system="", system_volatile="", history=[], user="u")
    assert [e["text"] for e in openai_llm.run(r) if e["type"] == "delta"] == ["Which product?"]


def test_no_ack_for_read_only_tools(monkeypatch, tmp_path):
    look = {"type": "tool", "name": "get_cart_total", "args": {}, "ok": True, "ms": 1.0, "changes_cart": False, "result": {}}
    chat = _fake_provider(monkeypatch, tmp_path, [_round(1), look, {"type": "delta", "text": "You're welcome."}, _round(2)])
    evs = list(chat.run_turn("s-ro", "thank you, what's my total", chat.ChatSettings(provider="fake", latency_mode="fast"),
                             "k", "c-ro", lambda q, k=5, rerank=False: {"hits": [], "timings_ms": {"total": 0}}))
    assert [e["text"] for e in evs if e["type"] == "delta"] == ["You're welcome."]
