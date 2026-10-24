"""Voice pipeline tests: real WebSocket endpoint, fake ASR / LLM / TTS providers (no network)."""

import json
import struct
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import backend.tools.registry as reg
from backend import chat
from backend.llm import PROVIDERS
from backend.memory import Memory
from backend.tools.cart import CartStore
from backend.voice import audio, session as vsession
from backend.voice.session import SentenceSplitter


# ------------------------------------------------------------------ units

def test_splitter_releases_first_sentence_early():
    sp = SentenceSplitter()
    out = []
    for tok in "Sure! Nimbus CRM Professional costs $45 per user per month. Annual billing saves 20 percent. Want it?".split(" "):
        out += sp.push(tok + " ")
    out.append(sp.flush())
    assert out[0] == "Sure! Nimbus CRM Professional costs $45 per user per month."  # "Sure!" alone is too short
    assert " ".join(out).split() == "Sure! Nimbus CRM Professional costs $45 per user per month. Annual billing saves 20 percent. Want it?".split()


def test_splitter_breaks_run_on_text():
    sp = SentenceSplitter(max_len=50)
    chunks = sp.push("word " * 30)
    assert chunks and all(len(c) <= 50 for c in chunks)


def test_resample_and_wav():
    tone = (np.sin(np.arange(16000) / 5) * 8000).astype("<i2").tobytes()  # 1 s at 16 kHz
    up = audio.resample(tone, 16000, 24000)
    assert len(up) == 24000 * 2
    assert audio.wav_bytes(tone)[:4] == b"RIFF"
    assert audio.duration_ms(up, 24000) == 1000


# ------------------------------------------------------------------ websocket pipeline

class FakeLLM:
    MODELS = {"fake-1": {"label": "Fake", "tier": "lite"}}
    DEFAULT = LITE = "fake-1"

    def run(self, req):
        for word in "Nimbus CRM costs $15 per user per month. It has a free tier too. Anything else?".split(" "):
            yield {"type": "delta", "text": word + " "}
        yield {"type": "round", "n": 1, "ttft_ms": 3.0, "ms": 9.0,
               "usage": {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 0}}

    def complete(self, *a):
        return "summary"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setitem(PROVIDERS, "fake", {"label": "Fake", "module": FakeLLM()})
    monkeypatch.setattr(reg, "store", CartStore(tmp_path / "carts.json"))
    monkeypatch.setattr(chat, "memory", Memory())
    monkeypatch.setattr(vsession, "api_key", lambda p: "test-key")
    tts_calls = []

    def fake_stream(provider, text, voice, key, **kw):
        tts_calls.append(("stream", text))
        yield b"\x01\x00" * 2400 + b"\x02"  # odd length on purpose: must not split samples
        yield b"\x00" + b"\x03\x00" * 2400

    def fake_batch(provider, text, voice, key):
        tts_calls.append(("batch", text))
        return b"\x04\x00" * 4800

    monkeypatch.setattr(vsession.tts, "stream", fake_stream)
    monkeypatch.setattr(vsession.tts, "batch", fake_batch)
    monkeypatch.setattr(vsession.asr, "batch", lambda provider, pcm, key: "how much is the crm" if pcm else "")
    from backend.app import app

    c = TestClient(app)
    c.tts_calls = tts_calls
    return c


def start_msg(mode, asr_provider="openai"):
    return {"type": "start", "cart_id": "voice-test-cart", "session_id": "voice-test-sess",
            "settings": {"mode": mode, "asr_provider": asr_provider, "tts_provider": "openai",
                         "chat": {"provider": "fake", "tools_enabled": []}}}


def collect(ws, until="turn_done", limit=500):
    msgs, audio_frames = [], []
    for _ in range(limit):
        m = ws.receive()
        if m.get("bytes") is not None:
            audio_frames.append(m["bytes"])
            continue
        ev = json.loads(m["text"])
        msgs.append(ev)
        if ev["type"] in (until, "error"):
            break
    return msgs, audio_frames


def test_batch_pipeline_from_mic_audio(client):
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("batch")))
        assert collect(ws, "ready")[0][-1]["type"] == "ready"
        ws.send_text(json.dumps({"type": "speech_start"}))
        for _ in range(10):
            ws.send_bytes(b"\x10\x00" * 320)  # 20 ms frames
        ws.send_text(json.dumps({"type": "speech_end", "endpoint_ms": 700}))
        msgs, frames = collect(ws)
    types = [m["type"] for m in msgs]
    assert types[0] == "asr_final" and msgs[0]["text"] == "how much is the crm"
    assert "tts_start" in types and "tts_end" in types and types[-1] == "turn_done"
    assert client.tts_calls == [("batch", "Nimbus CRM costs $15 per user per month. It has a free tier too. Anything else?")]
    done = msgs[-1]
    assert done["mode"] == "batch" and done["timings_ms"]["endpoint"] == 700
    assert done["audio_ms"] == 200  # 4800 samples at 24 kHz
    assert all(struct.unpack("<I", f[:4])[0] == 1 and (len(f) - 4) % 2 == 0 for f in frames)


def test_streaming_pipeline_speaks_sentence_by_sentence(client):
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("streaming", asr_provider="browser")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "text", "text": "how much is the crm", "asr_ms": 120, "endpoint_ms": 500}))
        msgs, frames = collect(ws)
    spoken = [t for kind, t in client.tts_calls if kind == "stream"]
    assert spoken == ["Nimbus CRM costs $15 per user per month.", "It has a free tier too. Anything else?"]
    done = msgs[-1]
    assert done["type"] == "turn_done" and done["timings_ms"]["asr"] == 120
    assert done["timings_ms"]["server_first_audio"] is not None
    assert sum(len(f) - 4 for f in frames) % 2 == 0 and all((len(f) - 4) % 2 == 0 for f in frames)
    deltas = "".join(m["text"] for m in msgs if m["type"] == "delta")
    assert deltas.startswith("Nimbus CRM costs")


def test_empty_transcript(client):
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("batch")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "speech_start"}))
        ws.send_text(json.dumps({"type": "speech_end", "endpoint_ms": 700}))
        msgs, _ = collect(ws, "asr_empty")
    assert msgs[-1]["type"] == "asr_empty"


# ------------------------------------------------------------------ barge-in (M6)

from backend.voice.session import spoken_text

SECOND = 24000 * 2  # bytes of 24 kHz PCM16 per second


def test_spoken_text_maps_played_audio_to_words():
    segs = [["One two three four.", SECOND, True], ["Five six seven eight.", SECOND, True]]
    assert spoken_text(segs, 0) == ""
    assert spoken_text(segs, 500) == "One two"                        # half of the first sentence
    assert spoken_text(segs, 1000) == "One two three four."
    assert spoken_text(segs, 1750) == "One two three four. Five six seven"
    assert spoken_text(segs, 5000) == "One two three four. Five six seven eight."
    # Still synthesizing: 0.25 s produced of a 28-char sentence (~2 s at 14 chars/s) -> estimate from text
    unfinished = [["One two three four five six.", SECOND // 4, False]]
    assert spoken_text(unfinished, 1000) == "One two three"


@pytest.fixture
def slow_client(client, monkeypatch):
    def slow_stream(provider, text, voice, key, **kw):  # 1 s of audio per sentence, delivered over ~0.6 s
        for _ in range(4):
            time.sleep(0.15)
            yield b"\x05\x00" * 6000
    monkeypatch.setattr(vsession.tts, "stream", slow_stream)
    return client


def last_turn():
    conv = chat.memory.get("voice-test-sess")
    return conv.turns[-1] if conv.turns else None


def test_barge_in_mid_reply_keeps_only_what_was_heard(slow_client):
    with slow_client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("streaming", asr_provider="browser")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "text", "text": "how much is the crm"}))
        frames = 0
        while frames < 3:  # 750 ms of the first sentence's audio has arrived
            frames += ws.receive().get("bytes") is not None
        ws.send_text(json.dumps({"type": "interrupt", "turn": 1, "played_ms": 500}))
        msgs, _ = collect(ws, "interrupted")
    ev = msgs[-1]
    assert ev["type"] == "interrupted" and ev["played_ms"] == 500
    first = "Nimbus CRM costs $15 per user per month."
    # 500 ms into a 41-char sentence still being synthesized (~2.9 s at 14 chars/s): only its first word.
    assert ev["spoken"] == "Nimbus" and first.startswith(ev["spoken"])
    assert ev["reply"].startswith(first)
    assert "turn_done" not in [m["type"] for m in msgs]
    t = last_turn()
    assert t.user == "how much is the crm" and t.cancelled and t.assistant == "Nimbus"
    assert t.assistant_text() == "Nimbus [cancelled by the user]"
    assert len(chat.memory.get("voice-test-sess").turns) == 1  # no stray full reply stored as well


def test_barge_in_after_server_finished_trims_stored_reply(client):
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("streaming", asr_provider="browser")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "text", "text": "how much is the crm"}))
        collect(ws, "turn_done")
        assert not last_turn().cancelled  # full reply stored
        # Each fake sentence is 4801 samples = ~200 ms; 250 ms in = first sentence plus a bit.
        ws.send_text(json.dumps({"type": "interrupt", "turn": 1, "played_ms": 250}))
        msgs, _ = collect(ws, "interrupted")
    t = last_turn()
    assert msgs[-1]["spoken"].startswith("Nimbus CRM costs $15 per user per month.")
    assert t.cancelled and t.assistant == msgs[-1]["spoken"]
    assert len(chat.memory.get("voice-test-sess").turns) == 1


def test_interrupt_for_unknown_turn_is_ignored(client):
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("streaming", asr_provider="browser")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "interrupt", "turn": 99, "played_ms": 100}))
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        msgs, _ = collect(ws, "turn_done")
    assert msgs[-1]["type"] == "turn_done"


def test_barge_in_while_transcribing_drops_the_utterance(client, monkeypatch):
    def slow_asr(provider, pcm, key):
        time.sleep(0.5)
        return "first question"
    monkeypatch.setattr(vsession.asr, "batch", slow_asr)
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps(start_msg("batch")))
        collect(ws, "ready")
        ws.send_text(json.dumps({"type": "speech_start"}))
        ws.send_bytes(b"\x10\x00" * 3200)
        ws.send_text(json.dumps({"type": "speech_end", "endpoint_ms": 700}))
        ws.send_text(json.dumps({"type": "interrupt", "turn": None, "played_ms": 0}))  # user talks again at once
        ws.send_text(json.dumps({"type": "text", "text": "second question"}))
        msgs, _ = collect(ws, "turn_done")
    assert "first question" not in [m.get("text") for m in msgs if m["type"] == "asr_final"]
    assert msgs[-1]["transcript"] == "second question"
    assert [t.user for t in chat.memory.get("voice-test-sess").turns] == ["second question"]


def test_asr_is_pinned_to_english_with_vocabulary(monkeypatch):
    from types import SimpleNamespace as NS
    from backend import clients
    from backend.voice import asr

    seen = {}
    fake = NS(audio=NS(transcriptions=NS(create=lambda **kw: seen.update(kw) or NS(text=" Add two CRM seats. "))))
    monkeypatch.setattr(clients, "openai", lambda key: fake)
    assert asr.batch("openai", b"\0\0" * 1600, "k") == "Add two CRM seats."
    assert seen["language"] == "en" and "Nimbus CRM" in seen["prompt"] and "Professional" in seen["prompt"]
    assert "language_code=eng" in asr.ELEVEN_RT_URL
