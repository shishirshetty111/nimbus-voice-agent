"""Conversation memory: the last N turns verbatim, everything older folded into a running summary.

Summaries are produced by the provider's lite model on a background thread after the reply has
been sent, so they never add latency to a turn.

Locally conversations live in this process. In production (Upstash Redis configured, backend/kv.py)
each one is also saved to Redis after every change and reloaded at the start of every turn, because
the next request, or a voice reconnect, may be served by a different server instance.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("nimbus")
CANCELLED = "[cancelled by the user]"
CONV_TTL_S = 7 * 24 * 3600  # Redis: a conversation nobody touched for a week expires


@dataclass
class Turn:
    user: str
    assistant: str
    actions: list[str] = field(default_factory=list)  # e.g. "add_to_cart(product=crm) -> ok"
    cancelled: bool = False
    ts: float = field(default_factory=time.time)

    def assistant_text(self) -> str:
        text = self.assistant + (f" {CANCELLED}" if self.cancelled else "")
        if self.actions:
            text += "\n[Actions taken this turn: " + "; ".join(self.actions) + "]"
        return text


@dataclass
class Conversation:
    id: str
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    summarized: int = 0           # turns[:summarized] are covered by the summary
    summarizing: bool = False
    last_tools: set = field(default_factory=set)  # Low-latency mode: tools offered last turn (sticky once)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def history(self, verbatim: int) -> list[dict]:
        """Messages for the LLM: the last `verbatim` turns, oldest first."""
        recent = self.turns[len(self.turns) - verbatim:] if verbatim else []
        msgs = []
        for t in recent:
            msgs += [{"role": "user", "content": t.user}, {"role": "assistant", "content": t.assistant_text()}]
        return msgs

    def context_summary(self, verbatim: int) -> str:
        """Summary covering turns older than the verbatim window. Turns that dropped out of the window
        but are not summarized yet (summary still running) are included as compact notes."""
        cutoff = max(0, len(self.turns) - verbatim)
        pending = self.turns[self.summarized:cutoff]
        notes = "\n".join(f"- User: {t.user} / Assistant: {t.assistant_text()}" for t in pending)
        return "\n".join(x for x in (self.summary, notes) if x)

    def to_json(self) -> dict:
        return {"turns": [asdict(t) for t in self.turns], "summary": self.summary,
                "summarized": self.summarized, "last_tools": sorted(self.last_tools)}

    def load_json(self, d: dict) -> None:
        self.turns = [Turn(**t) for t in d.get("turns", [])]
        self.summary, self.summarized = d.get("summary", ""), d.get("summarized", 0)
        self.last_tools = set(d.get("last_tools", []))

    def snapshot(self, verbatim: int) -> dict:
        cutoff = max(0, len(self.turns) - verbatim)
        return {"id": self.id, "turns": len(self.turns), "verbatim": len(self.turns) - cutoff,
                "summarized": self.summarized, "summarizing": self.summarizing, "summary": self.summary,
                "transcript": [{"user": t.user, "assistant": t.assistant_text(), "in_window": i >= cutoff,
                                "summarized": i < self.summarized} for i, t in enumerate(self.turns)]}


SUMMARY_SYSTEM = """You maintain a running summary of a customer's conversation with the Nimbus sales assistant.
Merge the new turns into the existing summary. Keep facts that matter later: the customer's needs and
team size, products and plans discussed, prices quoted, cart changes and orders, open questions and
decisions. Write 3-8 short sentences, third person, no preamble."""


class Memory:
    def __init__(self, max_conversations: int = 500, kv=None):
        self._convs: dict[str, Conversation] = {}
        self._lock = threading.Lock()
        self.max = max_conversations
        self.kv = kv

    def get(self, cid: str) -> Conversation:
        with self._lock:
            if cid not in self._convs:
                if len(self._convs) >= self.max:  # drop the oldest conversation
                    self._convs.pop(next(iter(self._convs)))
                self._convs[cid] = Conversation(cid)
            conv = self._convs[cid]
        if self.kv:  # another instance may have added turns since we last saw this conversation
            try:
                saved = self.kv.get_json(f"conv:{cid}")
                if saved is not None:
                    with conv.lock:
                        conv.load_json(saved)
            except Exception:
                log.exception("loading conversation %s failed; using this instance's copy", cid)
        return conv

    def save(self, conv: Conversation) -> None:
        """Persist after a change (no-op without Redis)."""
        if not self.kv:
            return
        try:
            with conv.lock:
                data = conv.to_json()
            self.kv.set_json(f"conv:{conv.id}", data, CONV_TTL_S)
        except Exception:
            log.exception("saving conversation %s failed", conv.id)

    def record_interrupted(self, cid: str, user: str, spoken: str, actions: list[str], already_recorded: bool) -> Turn:
        """Make memory match what the user actually heard. If the LLM had finished, its full reply is already
        stored as the last turn: cut it down. Otherwise nothing is stored yet: add the partial turn."""
        conv = self.get(cid)
        with conv.lock:
            last = conv.turns[-1] if conv.turns else None
            if already_recorded and last and last.user == user and not last.cancelled:
                last.assistant, last.cancelled = spoken, True
                turn = last
            else:
                turn = Turn(user=user, assistant=spoken, actions=actions, cancelled=True)
                conv.turns.append(turn)
        self.save(conv)
        return turn

    def drop_last_full(self, cid: str, user: str) -> None:
        """Remove a full (uncancelled) reply for `user` stored after a barge-in already recorded the partial."""
        conv = self.get(cid)
        with conv.lock:
            for i in range(len(conv.turns) - 1, -1, -1):
                if conv.turns[i].user == user and not conv.turns[i].cancelled:
                    del conv.turns[i]
                    break
            else:
                return
        self.save(conv)

    def reset(self, cid: str) -> None:
        with self._lock:
            self._convs.pop(cid, None)
        if self.kv:
            self.kv.delete(f"conv:{cid}")

    def maybe_summarize(self, conv: Conversation, verbatim: int, complete) -> None:
        """Fold turns that left the verbatim window into the summary, in the background.
        `complete(system, prompt) -> str` is the provider's lite-model completion."""
        cutoff = max(0, len(conv.turns) - verbatim)
        with conv.lock:
            if conv.summarizing or cutoff <= conv.summarized:
                return
            conv.summarizing = True
            todo, start = conv.turns[conv.summarized:cutoff], conv.summarized

        def work():
            try:
                turns = "\n".join(f"User: {t.user}\nAssistant: {t.assistant_text()}" for t in todo)
                prompt = f"Existing summary:\n{conv.summary or '(none)'}\n\nNew turns to merge:\n{turns}"
                new = complete(SUMMARY_SYSTEM, prompt)
                with conv.lock:
                    changed = conv.summarized == start  # nobody reset or summarized meanwhile
                    if changed:
                        conv.summary, conv.summarized = new, start + len(todo)
                if changed:
                    self.save(conv)
            except Exception:
                log.exception("summary failed; turns stay as compact notes")
            finally:
                conv.summarizing = False

        threading.Thread(target=work, daemon=True).start()


def _shared_kv():
    from backend import kv

    return kv.client()


memory = Memory(kv=_shared_kv())
