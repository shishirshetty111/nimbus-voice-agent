"""Run the real app with scripted ASR / LLM / TTS so the browser voice loop can be tested without API keys.

    ..\\.venv\\Scripts\\python -m tests.fake_server        (serves http://localhost:8099)

Select provider "Fake" in the Playground. Every reply is three sentences of one second of audio each.
Delays mimic real providers so streaming vs batch differ: ~30 words/s from the LLM, 250 ms to first TTS audio.
FAKE_LLM_DELAY=<seconds> (default 0.3) sets the LLM's time to first token, e.g. 1.5 to make fillers fire.
"""

import os
import time

import uvicorn

from backend import app as appmod
from backend.llm import PROVIDERS
from backend.voice import session as vsession

REPLY = ("Nimbus CRM helps your team close deals faster. It tracks every contact and deal in one place. "
         "Paid plans start at fifteen dollars per user per month.")


class FakeLLM:
    MODELS = {"fake-1": {"label": "Fake (scripted reply)", "tier": "lite"}}
    DEFAULT = LITE = "fake-1"

    def run(self, req):
        time.sleep(float(os.getenv("FAKE_LLM_DELAY", "0.3")))  # time to first token
        for word in REPLY.split(" "):
            time.sleep(0.033)
            yield {"type": "delta", "text": word + " "}
        yield {"type": "round", "n": 1, "ttft_ms": 1.0, "ms": 2.0,
               "usage": {"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0}}

    def complete(self, *a):
        return "summary"


def fake_tts_stream(provider, text, voice, key, **kw):
    time.sleep(0.25)  # time to first audio byte
    for i in range(10):  # 1 s per sentence: a 220 Hz-ish square wave so it is audible if played
        yield (b"\x00\x10" * 27 + b"\x00\xf0" * 27) * 44 + b"\x00\x10" * 24


PROVIDERS["fake"] = {"label": "Fake", "module": FakeLLM()}
vsession.tts.stream = fake_tts_stream
vsession.tts.batch = lambda p, t, v, k: b"".join(fake_tts_stream(p, t, v, k))
vsession.asr.batch = lambda provider, pcm, key: "tell me about nimbus crm"
vsession.asr.STREAMING = {}  # no real provider sockets: every utterance uses the scripted batch ASR
vsession.api_key = lambda provider: "fake-key"
appmod.api_key = lambda provider: "fake-key"  # typed chat (/api/chat) resolves keys here

if __name__ == "__main__":
    uvicorn.run(appmod.app, port=8099, log_level="warning")
