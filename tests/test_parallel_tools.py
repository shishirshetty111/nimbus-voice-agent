"""Parallel tool calls: independent calls in one round overlap; cart-changing rounds stay in order."""

import json
import threading
import time
from types import SimpleNamespace as NS

from backend.llm import base, openai_llm
from backend.llm.base import TurnRequest, execute_calls
from backend.tools.registry import BY_NAME


def slow_executor(delay=0.2, log=None):
    def execute(name, args):
        start = time.perf_counter()
        time.sleep(delay)
        if log is not None:
            log.append((name, start, threading.current_thread().name))
        return {"name": name, "args": args, "ok": True, "ms": delay * 1000, "changes_cart": BY_NAME[name].changes_cart,
                "result": {"tool": name}}
    return execute


def req(execute):
    return TurnRequest(api_key="k", model="m", system="", system_volatile="", history=[], user="u", execute=execute)


def test_independent_calls_run_concurrently_in_order():
    log = []
    outs, wall = execute_calls(req(slow_executor(0.2, log)),
                               [{"name": "annual_price", "args": {"product": "crm"}},
                                {"name": "annual_savings", "args": {"product": "crm"}},
                                {"name": "top_k_expensive", "args": {}}])
    assert [o["name"] for o in outs] == ["annual_price", "annual_savings", "top_k_expensive"]  # call order kept
    assert wall < 400                          # three 200 ms calls overlapped, not 600 ms in a row
    assert all(o["parallel"] for o in outs)
    assert len({thread for _, _, thread in log}) == 3


def test_cart_changes_keep_the_round_sequential():
    log = []
    outs, wall = execute_calls(req(slow_executor(0.1, log)),
                               [{"name": "add_to_cart", "args": {"product": "crm"}},
                                {"name": "get_cart_total", "args": {}}])
    starts = [start for _, start, _ in log]
    assert [o["name"] for o in outs] == ["add_to_cart", "get_cart_total"]
    assert starts[1] >= starts[0] + 0.09       # the read waited for the add
    assert wall >= 190 and not any(o["parallel"] for o in outs)


def test_prevalidated_failures_pass_through_in_position():
    bad = {"name": "annual_price", "args": "{oops", "ok": False, "ms": 0, "changes_cart": False, "result": {"error": "bad json"}}
    outs, _ = execute_calls(req(slow_executor(0.05)),
                            [{"name": "annual_price", "args": "{oops", "result": bad},
                             {"name": "top_k_expensive", "args": {}}])
    assert outs[0] is bad and outs[1]["name"] == "top_k_expensive"


def test_tool_round_event():
    ev = base.tool_round_event([{"parallel": True}, {"parallel": True}], 123.4)
    assert ev == {"type": "tool_round", "n": 2, "ms": 123.4, "parallel": True}


# ------------------------------------------------------------------ OpenAI adapter, mocked client

class FakeResponses:
    """Round 1: the model asks for two tools at once. Round 2: it answers."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=10, output_tokens=5, input_tokens_details=NS(cached_tokens=0))
        if len(self.calls) == 1:
            items = [NS(type="function_call", name=n, arguments=json.dumps({"product": "crm"}), call_id=f"call_{n}",
                        model_dump=lambda exclude_none=True, n=n: {"type": "function_call", "name": n, "call_id": f"call_{n}"})
                     for n in ("annual_price", "annual_savings")]
            return iter([NS(type="response.completed", response=NS(output=items, usage=usage))])
        return iter([NS(type="response.output_text.delta", delta="CRM is $45."),
                     NS(type="response.completed", response=NS(output=[], usage=usage))])


def test_openai_adapter_runs_one_round_of_parallel_tools(monkeypatch):
    fake = FakeResponses()
    monkeypatch.setattr(openai_llm, "_client", lambda key: NS(responses=fake))
    log = []
    r = TurnRequest(api_key="k", model="gpt-6-luna", system="s", system_volatile="", history=[], user="price and savings?",
                    tools=[BY_NAME["annual_price"], BY_NAME["annual_savings"]], execute=slow_executor(0.2, log))
    events = list(openai_llm.run(r))
    kinds = [e["type"] for e in events]
    assert fake.calls[0]["parallel_tool_calls"] is True
    assert kinds.count("tool") == 2 and kinds.count("tool_round") == 1 and kinds.count("round") == 2
    assert next(e for e in events if e["type"] == "tool_round")["ms"] < 350     # overlapped
    outputs = [i for i in fake.calls[1]["input"] if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["call_annual_price", "call_annual_savings"]  # both results, in order
    assert events[-1] == {"type": "round", **{k: events[-1][k] for k in ("n", "ttft_ms", "ms", "usage")}}


def test_chat_reports_round_wall_time_not_sum(monkeypatch, tmp_path):
    import backend.tools.registry as reg
    from backend import chat
    from backend.llm import PROVIDERS
    from backend.memory import Memory
    from backend.tools.cart import CartStore

    class TwoToolsAtOnce:
        MODELS = {"fake-1": {"label": "Fake"}}
        DEFAULT = LITE = "fake-1"

        def run(self, r):
            for name in ("annual_price", "top_k_expensive"):
                yield {"type": "tool", "name": name, "args": {}, "ok": True, "ms": 200.0, "changes_cart": False,
                       "result": {}, "parallel": True}
            yield {"type": "tool_round", "n": 2, "ms": 210.0, "parallel": True}
            yield {"type": "delta", "text": "Done."}

        def complete(self, *a):
            return ""

    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": TwoToolsAtOnce()})
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "c.json"))
    monkeypatch.setattr(chat, "memory", Memory())
    done = list(chat.run_turn("sess-par-1", "hi", chat.ChatSettings(provider="fake", tools_enabled=[]), "k", "cart-par-1",
                              lambda q, k=5, rerank=False: {"hits": [], "timings_ms": {"total": 0}}))[-1]
    assert done["timings_ms"]["tools"] == 210.0 and done["tool_calls"] == 2   # wall time, not 400 ms
