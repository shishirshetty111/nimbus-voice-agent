"""Speech-to-text. Mic audio arrives as 16 kHz mono PCM16.

batch(provider, pcm, key) transcribes a finished utterance in one request.
StreamingASR sessions receive audio while the user is still talking, so only a short
finalization step remains after they stop. Browser ASR (Web Speech API) runs client-side.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable

from backend import clients
from backend.voice.audio import MIC_RATE, resample, wav_bytes

PROVIDERS = {
    "browser": {"label": "Browser (Web Speech API)", "batch": "Web Speech final result",
                "streaming": "Web Speech interim results"},
    "openai": {"label": "OpenAI", "batch": "gpt-transcribe (file upload)",
               "streaming": "gpt-live-transcribe (Realtime WebSocket)"},
    "elevenlabs": {"label": "ElevenLabs", "batch": "Scribe v2 (file upload)",
                   "streaming": "Scribe v2 realtime (WebSocket)"},
    "gemini": {"label": "Google Gemini", "batch": "Gemini 3.5 Flash-Lite (audio prompt)",
               "streaming": "Gemini Live input transcription (side channel of a live session)"},
}

OPENAI_RT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
OPENAI_RT_RATE = 24_000  # the Realtime transcription session takes 24 kHz PCM
ELEVEN_RT_URL = ("wss://api.elevenlabs.io/v1/speech-to-text/realtime"
                 "?model_id=scribe_v2_realtime&audio_format=pcm_16000&commit_strategy=manual&language_code=eng")
GEMINI_LIVE_MODEL = "gemini-3.8-live"

# English only. Without this the recognizers auto-detect the language and sometimes hear an English
# sentence as Spanish ("Te haré un profesor..."), which the LLM then answers in Spanish.
LANGUAGE = "en"


def vocabulary() -> str:
    """A hint with the words this agent mostly hears: product and plan names and cart phrases."""
    from backend import catalog

    names = ", ".join(p["name"] for p in catalog.products())
    return (f"English. A customer talking to the Nimbus sales assistant about {names}; plans Free, Starter, "
            "Professional and Enterprise; seats, users, cart, checkout, annual or monthly billing. "
            "Example: \"Add two CRM Professional seats to the cart.\"")
FINAL_TIMEOUT = 8.0  # seconds to wait for a final transcript after the user stops


# ------------------------------------------------------------------ batch

def batch(provider: str, pcm: bytes, key: str) -> str:
    if provider == "openai":
        r = clients.openai(key).audio.transcriptions.create(
            model="gpt-transcribe", file=("speech.wav", wav_bytes(pcm), "audio/wav"),
            language=LANGUAGE, prompt=vocabulary())
        return r.text.strip()
    if provider == "elevenlabs":
        r = clients.http().post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": key},
                       data={"model_id": "scribe_v2", "file_format": "pcm_s16le_16", "language_code": "eng"},
                       files={"file": ("speech.pcm", pcm, "application/octet-stream")}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs STT {r.status_code}: {r.text[:200]}")
        return r.json().get("text", "").strip()
    if provider == "gemini":
        from google.genai import types

        r = clients.gemini(key).models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=[types.Part.from_bytes(data=wav_bytes(pcm), mime_type="audio/wav"),
                      "Transcribe this English speech exactly. Reply with the transcript only, no quotes or commentary. "
                      "If there is no speech, reply with nothing. Context: " + vocabulary()],
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="low")))
        return (r.text or "").strip()
    raise ValueError(f"no server-side ASR for {provider!r}")


# ------------------------------------------------------------------ streaming

class StreamingASR:
    """Base: open() when speech starts, send() audio frames, finish() -> final transcript.

    finish(quick=seconds) (Low-latency mode) waits that long for the provider's final transcript and
    then settles for the latest partial, if there is one; `used_partial` records which was used."""

    used_partial = False

    def __init__(self, key: str, on_partial: Callable[[str], None]):
        self.key, self.on_partial = key, on_partial

    def latest(self) -> str:
        return ""

    async def await_final(self, fut: asyncio.Future, quick: float | None) -> str:
        if quick:
            try:
                return await asyncio.wait_for(asyncio.shield(fut), quick)
            except asyncio.TimeoutError:
                if self.latest().strip():
                    self.used_partial = True
                    return self.latest()
        return await asyncio.wait_for(fut, FINAL_TIMEOUT)

    async def open(self): ...
    async def send(self, pcm: bytes): ...
    async def finish(self) -> str: ...
    async def close(self): ...


class OpenAIStreamingASR(StreamingASR):
    async def open(self):
        from websockets.asyncio.client import connect

        self.ws = await connect(OPENAI_RT_URL, additional_headers={"Authorization": f"Bearer {self.key}"},
                                max_size=None)
        await self.ws.send(json.dumps({"type": "session.update", "session": {
            "type": "transcription",
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": OPENAI_RT_RATE},
                                "transcription": {"model": "gpt-live-transcribe", "language": LANGUAGE,
                                                  "prompt": vocabulary()},
                                "noise_reduction": {"type": "near_field"},
                                "turn_detection": None}}}}))
        self.partial, self.final = "", asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t == "conversation.item.input_audio_transcription.delta":
                    self.partial += ev.get("delta", "")
                    self.on_partial(self.partial)
                elif t == "conversation.item.input_audio_transcription.completed" and not self.final.done():
                    self.final.set_result(ev.get("transcript", self.partial))
                elif t == "error" and not self.final.done():
                    self.final.set_exception(RuntimeError(f"OpenAI realtime: {ev.get('error', {}).get('message', ev)}"))
        except Exception as e:
            if not self.final.done():
                self.final.set_exception(RuntimeError(f"OpenAI realtime connection: {e}"))

    async def send(self, pcm: bytes):
        audio = base64.b64encode(resample(pcm, MIC_RATE, OPENAI_RT_RATE)).decode()
        await self.ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio}))

    def latest(self) -> str:
        return self.partial

    async def finish(self, quick: float | None = None) -> str:
        await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        return (await self.await_final(self.final, quick)).strip()

    async def close(self):
        self.reader.cancel()
        await self.ws.close()


class ElevenLabsStreamingASR(StreamingASR):
    async def open(self):
        from websockets.asyncio.client import connect

        self.ws = await connect(ELEVEN_RT_URL, additional_headers={"xi-api-key": self.key}, max_size=None)
        self.committed, self.final = [], asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                ev = json.loads(raw)
                t = ev.get("message_type", "")
                if t == "partial_transcript":
                    self.last_partial = ev.get("text", "")
                    self.on_partial(self.latest())
                elif t in ("committed_transcript", "committed_transcript_with_timestamps"):
                    self.committed.append(ev.get("text", ""))
                    if self.finishing and not self.final.done():
                        self.final.set_result(" ".join(self.committed))
                elif "error" in t and not self.final.done():
                    self.final.set_exception(RuntimeError(f"ElevenLabs realtime: {ev.get('error') or ev}"))
        except Exception as e:
            if not self.final.done():
                self.final.set_exception(RuntimeError(f"ElevenLabs realtime connection: {e}"))

    finishing = False
    last_partial = ""

    def latest(self) -> str:
        return " ".join([*self.committed, self.last_partial]).strip()

    async def send(self, pcm: bytes, commit: bool = False):
        await self.ws.send(json.dumps({"message_type": "input_audio_chunk", "audio_base_64": base64.b64encode(pcm).decode(),
                                       "commit": commit, "sample_rate": MIC_RATE}))

    async def finish(self, quick: float | None = None) -> str:
        self.finishing = True
        await self.send(b"", commit=True)
        return (await self.await_final(self.final, quick)).strip()

    async def close(self):
        self.reader.cancel()
        await self.ws.close()


class GeminiStreamingASR(StreamingASR):
    """Gemini has no transcription-only stream. A Live session transcribes the input as a side channel;
    the model is told to stay silent and its reply, if any, is ignored."""

    async def open(self):
        self.client = clients.gemini(self.key)
        self.cm = self.client.aio.live.connect(model=GEMINI_LIVE_MODEL, config={
            "response_modalities": ["AUDIO"], "input_audio_transcription": {},
            "speech_config": {"language_code": "en-US"},
            "system_instruction": "You are a silent transcription relay for English speech. Never reply; "
                                  "produce no output."})
        self.session = await self.cm.__aenter__()
        self.text, self.final = "", asyncio.get_running_loop().create_future()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for msg in self.session.receive():
                sc = getattr(msg, "server_content", None)
                it = getattr(sc, "input_transcription", None) if sc else None
                if it and it.text:
                    self.text += it.text
                    self.on_partial(self.text.strip())
                # The model starting its own turn means the input is fully transcribed.
                if sc and (getattr(sc, "model_turn", None) or getattr(sc, "turn_complete", False)) \
                        and self.ending and not self.final.done():
                    self.final.set_result(self.text)
        except Exception as e:
            if not self.final.done():
                self.final.set_exception(RuntimeError(f"Gemini Live: {e}"))

    ending = False

    async def send(self, pcm: bytes):
        from google.genai import types

        await self.session.send_realtime_input(audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={MIC_RATE}"))

    def latest(self) -> str:
        return self.text

    async def finish(self, quick: float | None = None) -> str:
        self.ending = True
        await self.session.send_realtime_input(audio_stream_end=True)
        try:
            return (await self.await_final(self.final, quick)).strip()
        except asyncio.TimeoutError:
            return self.text.strip()  # use whatever was transcribed

    async def close(self):
        self.reader.cancel()
        await self.cm.__aexit__(None, None, None)


STREAMING = {"openai": OpenAIStreamingASR, "elevenlabs": ElevenLabsStreamingASR, "gemini": GeminiStreamingASR}
