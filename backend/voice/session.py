"""Voice WebSocket session: mic audio in, ASR -> LLM (+tools) -> TTS, audio out, per-stage timings.

Two pipelines, chosen per session:
  batch      each stage waits for the previous one to finish (whole utterance -> whole reply -> whole clip)
  streaming  audio is transcribed while the user talks; the reply is split into sentences as the LLM
             writes it, and each sentence is synthesized and streamed out while the LLM keeps going

Protocol (JSON text frames unless noted)
  client -> server  start{settings, keys, cart_id, session_id} | update{settings}
                    speech_start | <binary PCM16 16 kHz> | speech_end{endpoint_ms}
                    text{text, asr_ms?, endpoint_ms?}   (browser ASR or typed input) | stop
                    interrupt{turn, played_ms}          (barge-in: the user talked over the reply)
  server -> client  ready | asr_partial{text} | asr_final{text, ms} | asr_empty
                    chat events (rag, delta, tool, round, error, done)
                    tts_start{turn, rate} | <binary: 4-byte LE turn id + PCM16 24 kHz> | tts_end{turn}
                    turn_done{turn, timings, ...} | interrupted{turn, played_ms, spoken, reply} | notice{message} | error{message}
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import WebSocket, WebSocketDisconnect

from backend import chat
from backend.config import PRODUCTION, api_key
from backend.llm.base import error_message
from backend.voice import asr, tts
from backend.voice.audio import TTS_RATE

log = logging.getLogger("nimbus")

DEFAULTS = {"mode": "streaming", "asr_provider": "browser", "tts_provider": "openai", "tts_voice": None}


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


class SentenceSplitter:
    """Cuts streaming LLM text into speakable chunks. The first chunk is released early (short
    minimum) because it decides how soon the user hears anything. With first_clause (Low-latency
    mode) the first chunk may also end at a comma or dash once it has four words."""

    BOUNDARY = re.compile(r"(?<=[.!?;:])\s+")
    CLAUSE = re.compile(r"(?<=[,\u2014\u2013])\s+|\s+-\s+")

    def __init__(self, first_min: int = 12, min_len: int = 40, max_len: int = 240, first_clause: bool = False):
        self.buf, self.first, self.min, self.max = "", True, (first_min, min_len), max_len
        self.first_clause = first_clause

    def push(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            need = self.min[0] if self.first else self.min[1]
            cut = next((m.end() for m in self.BOUNDARY.finditer(self.buf) if m.start() >= need), None)
            if cut is None and self.first and self.first_clause:
                cut = next((m.end() for m in self.CLAUSE.finditer(self.buf)
                            if len(self.buf[:m.start()].split()) >= 4), None)
            if cut is None and len(self.buf) > self.max:  # long run-on: break at the last comma/space
                cut = max(self.buf.rfind(", ", 0, self.max), self.buf.rfind(" ", 0, self.max)) + 1 or self.max
            if cut is None:
                return out
            chunk, self.buf = self.buf[:cut].strip(), self.buf[cut:]
            if chunk:
                out.append(chunk)
                self.first = False

    def flush(self) -> str:
        rest, self.buf = self.buf.strip(), ""
        return rest


SPEECH_CHARS_PER_S = 14  # typical TTS speaking rate, for sentences still being synthesized


def spoken_text(segments: list[list], played_ms: float) -> str:
    """What the user actually heard: whole sentences whose audio fully played, plus the words of the
    sentence that was cut off, estimated by how far into its audio playback got. Segments are
    [text, audio bytes, finished]; an unfinished sentence's full length is estimated from its text."""
    heard, t = [], 0.0
    for text, nbytes, *finished in segments:
        dur = nbytes / 2 / TTS_RATE * 1000
        if not (finished and finished[0]):
            dur = max(dur, len(text) / SPEECH_CHARS_PER_S * 1000)
        if dur and t + dur <= played_ms:
            heard.append(text)
            t += dur
            continue
        if dur and played_ms > t:
            words = text.split()
            k = int(len(words) * (played_ms - t) / dur)
            if k:
                heard.append(" ".join(words[:k]))
        break
    return " ".join(heard).strip()


class VoiceSession:
    def __init__(self, ws: WebSocket, retrieve):
        self.ws, self.retrieve = ws, retrieve
        self.settings: dict = dict(DEFAULTS)
        self.chat_settings = chat.ChatSettings()
        self.keys: dict = {}
        self.cart_id = self.session_id = ""
        self.audio = bytearray()
        self.stream_asr: asr.StreamingASR | None = None
        self.stream_open: asyncio.Task | None = None
        self.stream_flushed = False  # audio buffered while the provider socket opened has been sent
        self.stream_error: Exception | None = None  # provider stream broke mid-utterance: fall back to batch
        self.spare: tuple | None = None  # Low-latency mode: (provider, asr session, open task) opened ahead of speech
        self.turn_task: asyncio.Task | None = None
        self.turn_id = 0
        self.interrupt_played: float | None = None  # set while a barge-in cancels the current turn
        self.last_turn: dict | None = None  # finished turn whose audio may still be playing in the browser
        self.send_lock = asyncio.Lock()

    # ------------------------------------------------------------------ plumbing

    def key(self, provider: str) -> str | None:
        return (self.keys.get(provider) or "").strip() or api_key(provider)

    async def send(self, obj) -> None:
        async with self.send_lock:
            if isinstance(obj, (bytes, bytearray)):
                await self.ws.send_bytes(bytes(obj))
            else:
                await self.ws.send_text(json.dumps(obj, default=str))

    def apply(self, msg: dict) -> None:
        s = msg.get("settings") or {}
        for k in DEFAULTS:
            if k in s:
                self.settings[k] = s[k]
        if "chat" in s:
            self.chat_settings = chat.ChatSettings(**s["chat"])
        if "keys" in msg:
            self.keys = {k: v for k, v in (msg["keys"] or {}).items() if isinstance(v, str)}
        self.cart_id = msg.get("cart_id", self.cart_id)
        self.session_id = msg.get("session_id", self.session_id)

    async def run(self) -> None:
        try:
            while True:
                m = await self.ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    try:
                        await self.on_audio(m["bytes"])
                    except Exception as e:
                        log.warning("audio frame failed: %s", e)
                elif m.get("text"):
                    await self.on_message(json.loads(m["text"]))
        except WebSocketDisconnect:
            pass
        finally:
            await self.cancel_turn()
            await self.close_stream_asr()
            await self.drop_spare()

    async def on_message(self, msg: dict) -> None:
        t = msg.get("type")
        try:
            if t in ("start", "update"):
                self.apply(msg)
                chat.resolve(self.chat_settings)  # validate early
                await self.drop_spare()  # provider or mode may have changed
                await self.prepare_spare()
                await self.send({"type": "ready", "settings": self.settings})
            elif t == "speech_start":
                await self.on_speech_start()
            elif t == "speech_end":
                await self.on_speech_end(float(msg.get("endpoint_ms") or 0))
            elif t == "text":
                await self.start_turn(msg["text"], float(msg.get("asr_ms") or 0), float(msg.get("endpoint_ms") or 0),
                                      time.perf_counter())
            elif t == "stop":
                await self.cancel_turn()
            elif t == "interrupt":
                tid = msg.get("turn")  # None: barge-in before any reply audio (still transcribing/thinking)
                played = float(msg.get("played_ms") or 0)
                if self.turn_task and not self.turn_task.done() and tid in (None, self.turn_id):
                    self.interrupt_played = played
                    try:
                        await self.cancel_turn()
                    finally:
                        self.interrupt_played = None
                elif self.last_turn and tid is not None and self.last_turn["tid"] == int(tid):
                    # The server finished, but the browser was still playing buffered audio.
                    lt, self.last_turn = self.last_turn, None
                    await self.record_barge_in(tid, lt["text"], played, lt["segments"], lt["state"], None)
        except Exception as e:
            log.warning("voice message failed: %s", e)
            await self.send({"type": "error", "message": error_message(e)})

    # ------------------------------------------------------------------ ASR

    def streaming_asr(self) -> bool:
        streaming = self.settings["mode"] == "streaming" or self.fast  # Low-latency mode always streams
        return streaming and self.settings["asr_provider"] in asr.STREAMING

    @property
    def fast(self) -> bool:
        return self.chat_settings.latency_mode == "fast"

    def new_stream_asr(self) -> tuple:
        provider = self.settings["asr_provider"]
        key = self.key(provider)
        if not key:
            raise RuntimeError(f"No {asr.PROVIDERS[provider]['label']} key for speech recognition.")
        inst = asr.STREAMING[provider](key, lambda text: asyncio.ensure_future(self.send({"type": "asr_partial", "text": text})))
        return provider, inst, asyncio.create_task(inst.open())

    async def prepare_spare(self) -> None:
        """Low-latency mode: open the next utterance's streaming-ASR socket now, so the TLS + WebSocket
        handshake is done before the user speaks."""
        if self.fast and self.streaming_asr() and not self.spare:
            try:
                self.spare = self.new_stream_asr()
            except Exception:
                self.spare = None

    async def drop_spare(self) -> None:
        if self.spare:
            _, inst, task = self.spare
            self.spare = None
            try:
                if task.done() and not task.cancelled() and task.exception() is None:
                    await inst.close()
                else:
                    task.cancel()
            except Exception:
                pass

    async def on_speech_start(self) -> None:
        self.audio = bytearray()
        await self.close_stream_asr()
        if self.streaming_asr():
            spare, self.spare = self.spare, None
            usable = bool(spare and spare[0] == self.settings["asr_provider"]
                          and not (spare[2].done() and (spare[2].cancelled() or spare[2].exception())))
            if spare and not usable:
                self.spare = spare
                await self.drop_spare()
            _, inst, task = spare if usable else self.new_stream_asr()
            self.stream_asr, self.stream_flushed, self.stream_error = inst, False, None
            self.stream_open = task

    async def on_audio(self, pcm: bytes) -> None:
        self.audio += pcm
        if (self.stream_asr and not self.stream_error and self.stream_open and self.stream_open.done()
                and self.stream_open.exception() is None):
            try:
                await self.flush_or_send(pcm)
            except Exception as e:  # provider closed the socket (bad key, quota...): finish this turn in batch
                self.stream_error = e

    async def flush_or_send(self, pcm: bytes | None) -> None:
        """First call after the socket opens sends everything buffered so far; later calls send the frame."""
        if not self.stream_flushed:
            self.stream_flushed = True
            if self.audio:
                await self.stream_asr.send(bytes(self.audio))
        elif pcm:
            await self.stream_asr.send(pcm)

    async def on_speech_end(self, endpoint_ms: float) -> None:
        """Hand the utterance (and its streaming-ASR socket) to a task, so this receive loop stays free
        to hear a barge-in while we transcribe, think and speak."""
        t0 = time.perf_counter()  # the endpoint fired: everything after this is response latency
        stream = {"inst": self.stream_asr, "open": self.stream_open, "flushed": self.stream_flushed,
                  "error": self.stream_error}
        self.stream_asr = self.stream_open = self.stream_error = None
        await self.cancel_turn()
        self.last_turn = None
        self.turn_id += 1
        self.turn_task = asyncio.create_task(self.utterance(self.turn_id, bytes(self.audio), stream, endpoint_ms, t0))

    async def utterance(self, tid: int, pcm: bytes, stream: dict, endpoint_ms: float, t0: float) -> None:
        provider = self.settings["asr_provider"]
        inst = stream["inst"]
        try:
            text, how = "", "batch"
            if inst:
                try:
                    await stream["open"]
                    if stream["error"]:
                        raise stream["error"]
                    if not stream["flushed"]:  # short utterance: the socket opened after the last frame
                        await inst.send(pcm)
                    # Always wait for the final transcript. (Low-latency mode used to settle for the live
                    # partial after 300 ms; partials are uncorrected guesses and garbled what users said.)
                    text = await inst.finish(quick=None)
                    how = "streaming (partial)" if inst.used_partial else "streaming"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await self.send({"type": "notice", "message": f"Streaming ASR failed ({error_message(e)}); used batch instead."})
                    text = await asyncio.to_thread(asr.batch, provider, pcm, self.key(provider))
            else:
                key = self.key(provider)
                if not key:
                    raise RuntimeError(f"No {asr.PROVIDERS[provider]['label']} key for speech recognition.")
                text = await asyncio.to_thread(asr.batch, provider, pcm, key)
            asr_ms = _ms(t0)
            if not text:
                await self.send({"type": "asr_empty", "ms": asr_ms})
                return
            await self.send({"type": "asr_final", "text": text, "ms": asr_ms, "how": how})
        except asyncio.CancelledError:
            raise  # barge-in while transcribing: drop this utterance, the user is talking again
        except Exception as e:
            await self.send({"type": "error", "message": error_message(e)})
            return
        finally:
            if inst:
                # Close in the background: the close handshake can take seconds, and awaiting it here
                # delayed the LLM call by ~2 s on every turn.
                asyncio.ensure_future(self._close_quietly(inst, stream["open"]))
            asyncio.ensure_future(self.prepare_spare())  # warm socket for the next utterance
        await self.turn(tid, text, asr_ms, endpoint_ms, t0)

    @staticmethod
    async def _close_quietly(inst, opened: asyncio.Future) -> None:
        try:
            if opened.done() and not opened.cancelled() and opened.exception() is None:
                await inst.close()
            else:
                opened.cancel()
        except Exception:
            pass

    async def close_stream_asr(self) -> None:
        inst, task = self.stream_asr, self.stream_open
        self.stream_asr = self.stream_open = None
        if inst and task:
            try:
                if task.done() and task.exception() is None:
                    await inst.close()
                else:
                    task.cancel()
            except Exception:
                pass

    # ------------------------------------------------------------------ turn

    async def cancel_turn(self) -> None:
        if self.turn_task and not self.turn_task.done():
            self.turn_task.cancel()
            try:
                await self.turn_task
            except (asyncio.CancelledError, Exception):
                pass

    async def start_turn(self, text: str, asr_ms: float, endpoint_ms: float, t0: float) -> None:
        await self.cancel_turn()
        self.last_turn = None
        self.turn_id += 1
        self.turn_task = asyncio.create_task(self.turn(self.turn_id, text, asr_ms, endpoint_ms, t0))

    async def turn(self, tid: int, text: str, asr_ms: float, endpoint_ms: float, t0: float) -> None:
        text = chat.redact_keys(text)  # a typed message may hold a pasted key; it must not be stored
        s, cs = self.settings, self.chat_settings
        llm_key, tts_provider = self.key(cs.provider), s["tts_provider"]
        tts_key = self.key(tts_provider)
        voice = s.get("tts_voice") or tts.PROVIDERS[tts_provider]["default_voice"]
        if not llm_key or not tts_key:
            missing = cs.provider if not llm_key else tts_provider
            await self.send({"type": "error", "message": f"No {missing} API key: add one in the API keys panel."})
            return
        fast = cs.latency_mode == "fast"
        streaming = s["mode"] == "streaming" or fast  # Low-latency mode always streams
        loop = asyncio.get_running_loop()
        events: asyncio.Queue = asyncio.Queue()

        def emit(ev):
            try:
                loop.call_soon_threadsafe(events.put_nowait, ev)
            except RuntimeError:
                pass  # the session ended while a worker thread was still finishing
        stop = threading.Event()
        marks: dict = {}
        segments: list[list] = []   # [sentence, audio bytes produced] in playback order, for barge-in
        state = {"reply": [], "actions": [], "recorded": False, "partial_recorded": False}

        def tts_say(sentence: str, synth) -> None:
            t = time.perf_counter()
            marks.setdefault("tts_start", t)
            seg = [sentence, 0, False]
            segments.append(seg)
            for chunk in synth(tts_provider, sentence, voice, tts_key) if streaming else [synth(tts_provider, sentence, voice, tts_key)]:
                if stop.is_set():
                    return
                if chunk:
                    marks.setdefault("tts_first_byte", time.perf_counter())
                    seg[1] += len(chunk)
                    emit(("audio", chunk))
            seg[2] = True
            marks["tts_end"] = time.perf_counter()

        sentences: queue.Queue = queue.Queue()

        def tts_worker():
            """Streaming: speak sentences while the LLM is still writing. Up to two sentences synthesize at
            once in Low-latency mode (one otherwise), always emitted in order, so the next sentence is
            ready when the current one finishes playing."""
            pool = ThreadPoolExecutor(max_workers=2 if fast else 1)
            order: queue.Queue = queue.Queue()

            def synth(sentence: str, out: queue.Queue) -> None:
                try:
                    for chunk in tts.stream(tts_provider, sentence, voice, tts_key, fast=fast):
                        if stop.is_set():
                            break
                        if chunk:
                            out.put(chunk)
                except Exception as e:
                    out.put(e)
                finally:
                    out.put(None)

            def feed() -> None:
                for sentence in iter(sentences.get, None):
                    if stop.is_set():
                        break
                    out: queue.Queue = queue.Queue()
                    marks.setdefault("tts_start", time.perf_counter())
                    pool.submit(synth, sentence, out)
                    order.put((sentence, out))
                order.put(None)

            threading.Thread(target=feed, daemon=True).start()
            try:
                for sentence, out in iter(order.get, None):
                    seg = [sentence, 0, False]
                    segments.append(seg)
                    for chunk in iter(out.get, None):
                        if isinstance(chunk, Exception):
                            raise chunk
                        if stop.is_set():
                            return
                        marks.setdefault("tts_first_byte", time.perf_counter())
                        seg[1] += len(chunk)
                        emit(("audio", chunk))
                    seg[2] = True
                    marks["tts_end"] = time.perf_counter()
            except Exception as e:
                emit({"type": "error", "message": f"TTS: {error_message(e)}"})
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                emit(("tts_done",))

        def llm_worker():
            splitter, reply = SentenceSplitter(first_clause=fast), state["reply"]
            marks["llm_start"] = time.perf_counter()
            try:
                for ev in chat.run_turn(self.session_id, text, cs, llm_key, self.cart_id, self.retrieve):
                    if ev["type"] == "done":
                        state["recorded"] = True  # run_turn stored the full reply in memory just before this
                        if stop.is_set() and state["partial_recorded"]:
                            # Barge-in already stored the partial turn while we were blocked: undo the full one.
                            chat.memory.drop_last_full(self.session_id, text)
                    if ev["type"] == "tool":
                        state["actions"].append(chat._action(ev))
                    if stop.is_set():
                        return
                    if ev["type"] == "delta":
                        marks.setdefault("llm_first_text", time.perf_counter())
                        reply.append(ev["text"])
                        if streaming and ev.get("ack") and not splitter.buf.strip():
                            sentences.put(ev["text"].strip())  # speak the tool acknowledgement right away
                        elif streaming:
                            for sentence in splitter.push(ev["text"]):
                                sentences.put(sentence)
                    emit(ev)
                marks["llm_end"] = time.perf_counter()
                if streaming:
                    rest = splitter.flush()
                    if rest:
                        sentences.put(rest)
                elif "".join(reply).strip():
                    tts_say("".join(reply).strip(), tts.batch)  # batch: synthesize the whole reply at once
            except Exception as e:
                emit({"type": "error", "message": str(e)})
            finally:
                sentences.put(None)
                emit(("llm_done",))
                if not streaming:
                    emit(("tts_done",))

        await self.send({"type": "tts_start", "turn": tid, "rate": TTS_RATE})
        llm_thread = threading.Thread(target=llm_worker, daemon=True)
        llm_thread.start()
        if streaming:
            threading.Thread(target=tts_worker, daemon=True).start()

        header, carry, pending, done = struct.pack("<I", tid), b"", {"llm_done", "tts_done"}, None
        first_audio_at, audio_bytes = None, 0
        try:
            while pending:
                ev = await events.get()
                if isinstance(ev, tuple):
                    if ev[0] == "audio":
                        data = carry + ev[1]
                        cut = len(data) // 2 * 2  # never split a 16-bit sample across frames
                        data, carry = data[:cut], data[cut:]
                        if data:
                            first_audio_at = first_audio_at or time.perf_counter()
                            audio_bytes += len(data)
                            await self.send(header + data)
                    else:
                        pending.discard(ev[0])
                    continue
                if ev["type"] == "done":
                    done = ev
                if ev["type"] == "tool" and ev.get("changes_cart"):
                    await self.send({"type": "cart_changed"})
                await self.send(ev)
        except asyncio.CancelledError:
            stop.set()
            if self.interrupt_played is not None:
                await self.record_barge_in(tid, text, self.interrupt_played, segments, state, llm_thread)
            raise
        await self.send({"type": "tts_end", "turn": tid})

        self.last_turn = {"tid": tid, "text": text, "segments": segments, "state": state}
        rel = lambda k: round((marks[k] - t0) * 1000, 1) if k in marks else None
        dur = lambda a, b: round((marks[b] - marks[a]) * 1000, 1) if a in marks and b in marks else None
        llm_t = (done or {}).get("timings_ms", {})
        log.warning("voice turn=%d profile=%s mode=%s asr=%s/%.0fms endpoint=%.0fms tts_wait=%sms tts_ttfb=%sms "
                    "first_audio=%sms sentences=%s", tid, cs.latency_mode, "streaming" if streaming else "batch",
                    s["asr_provider"], asr_ms, endpoint_ms, rel("tts_start"), dur("tts_start", "tts_first_byte"),
                    round((first_audio_at - t0) * 1000) if first_audio_at else None,
                    len(segments) if PRODUCTION else [x[0][:60] for x in segments])  # no speech text in prod logs
        await self.send({
            "type": "turn_done", "turn": tid, "mode": "streaming" if streaming else "batch", "transcript": text, "knowledge": cs.knowledge_mode,
            "profile": cs.latency_mode,
            "providers": {"asr": s["asr_provider"], "llm": f"{cs.provider}/{(done or {}).get('model')}",
                          "tts": f"{tts_provider}/{tts.PROVIDERS[tts_provider]['model']}"},
            "timings_ms": {
                # durations of each stage (ms); see M7 for how they add up to the total
                "endpoint": endpoint_ms,                       # silence waited before deciding the user was done
                "asr": asr_ms,                                 # endpoint -> final transcript
                "rag": llm_t.get("rag"),
                "llm_ttft": llm_t.get("llm_ttft"),             # first LLM round: request -> first token
                "llm_first_text": dur("llm_start", "llm_first_text"),
                "llm_total": dur("llm_start", "llm_end"),
                "tools": llm_t.get("tools"),
                "tools_only": llm_t.get("tools_only"),
                "tts_ttfb": dur("tts_start", "tts_first_byte"),  # first TTS request -> first audio byte
                "tts_wait": round((marks["tts_start"] - t0) * 1000, 1) if "tts_start" in marks else None,
                "server_first_audio": round((first_audio_at - t0) * 1000, 1) if first_audio_at else None,
                "server_total": _ms(t0),
            },
            "audio_ms": round(audio_bytes / 2 / TTS_RATE * 1000),
            "usage": (done or {}).get("usage"), "tool_calls": (done or {}).get("tool_calls"),
        })

    async def record_barge_in(self, tid, text, played_ms, segments, state, llm_thread) -> None:
        """The user talked over the reply: keep only what they heard, marked [cancelled by the user]."""
        if llm_thread:
            await asyncio.to_thread(llm_thread.join, 3)  # let the LLM thread notice the stop flag
        spoken = spoken_text(segments, played_ms)
        state["partial_recorded"] = True
        chat.memory.record_interrupted(self.session_id, text, spoken, state["actions"], state["recorded"])
        try:
            await self.send({"type": "interrupted", "turn": tid, "played_ms": round(played_ms),
                             "spoken": spoken, "reply": "".join(state["reply"]).strip()})
        except Exception:
            pass  # socket already gone
