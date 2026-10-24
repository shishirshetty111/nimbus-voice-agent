// Voice client: microphone -> 16 kHz PCM + endpoint detection -> /ws/voice -> TTS playback.
//
// End-of-speech detection is energy based. Speech starts when a 20 ms frame is clearly louder than the
// noise floor. The floor is tracked all the time: it follows the room down at once and up slowly, so a
// louder room (a fan, or the browser's automatic gain turning the mic up) is learned even mid-sentence.
// While the user talks, a frame counts as silence once it is well below *their* speaking level, so steady
// background noise can't hold the turn open. The turn ends after `endpoint_ms` of silence (the slider),
// or after MAX_UTTERANCE_MS no matter what.
// Barge-in: while the agent thinks or speaks, a stricter detector (louder, longer) keeps listening so
// its own voice leaking into the mic doesn't trigger it. Real speech flushes playback at once, tells the
// server how much of the reply was heard, and becomes the next utterance. All latency marks use
// performance.now() on this one clock.

import { apiBase } from "./api.js";
import { keyHeaders } from "./keys.js";

const FRAME = 320;              // 20 ms at 16 kHz
const PREROLL_FRAMES = 25;      // keep 500 ms before speech onset so the first word reaches ASR whole
const START_FRAMES = 3;         // 60 ms of speech before we call it speech
const MIN_SPEECH_MS = 250;
const START_MARGIN_DB = 15;     // speech starts this far above the noise floor
const FLOOR_RISE_DB = 0.06;     // per frame (3 dB/s): how fast the floor follows a louder room
const SILENCE_MARGIN_DB = 9;    // within this of the floor is silence...
const SPEECH_DROP_DB = 18;      // ...and so is anything this far below the user's own speaking level
const MAX_UTTERANCE_MS = 25000; // safety net: end the turn even if "speech" never stops
const BARGE_FRAMES = 8;         // 160 ms of speech over the agent before we stop it...
const BARGE_WINDOW = 15;        // ...within any 300 ms: echo cancellation chops the mic during playback
const BARGE_MARGIN_DB = 22;     // stricter than START (15 dB): echo of the agent's voice is quieter

// Low-latency mode (cfg.latency_mode === "fast"); see docs/latency-reduction-plan.md
// Adaptive end-of-speech wait (ms). 300 ms cut people off at natural mid-sentence pauses, so the
// shortest wait is 500 ms, used only when the live transcript already ends like a finished sentence.
const FAST_ENDPOINT = { done: 500, unknown: 700, trailing: 1000 };
const REFRESH_AFTER_MS = 4 * 60 * 1000; // swap voice sockets before Vercel's 5-minute limit
const TRAILING = /\b(and|or|but|so|because|um+|uh+|er+|like|the|a|an|to|of|for|with|in|on|my|your|is|are|if|that|then)\s*$/i;
const FAST_BUFFER = { start: 40, step: 40, max: 200, min: 40 };   // adaptive playback buffer (ms)
const FILLER_AFTER_MS = 700;                                        // play a filler if nothing has arrived by then
const FILLERS = ["One moment.", "Let me check.", "Sure."];
const fillerCache = new Map(); // "provider|voice|text" -> Int16Array (24 kHz), shared by every client on the page

export class VoiceClient {
  constructor(handlers) {
    this.h = handlers;          // {onState, onLevel, onPartial, onEvent, onTurnDone, onError}
    this.state = "off";
    this.cfg = { endpoint_ms: 700, buffer_ms: 120, asr_provider: "browser", mode: "streaming", barge_in: true };
  }

  // ------------------------------------------------------------------ lifecycle

  async start(startMsg, cfg) {
    Object.assign(this.cfg, cfg);
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule(new URL("./worklets.js", import.meta.url));
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.mic = new AudioWorkletNode(this.ctx, "mic-capture");
    this.mic.port.onmessage = ({ data }) => this.onMicSamples(data);
    src.connect(this.mic);
    this.player = new AudioWorkletNode(this.ctx, "pcm-player", { outputChannelCount: [1] });
    this.player.port.onmessage = ({ data }) => this.onPlayer(data);
    this.player.connect(this.ctx.destination);
    this.bufferMs = this.fast ? FAST_BUFFER.start : this.cfg.buffer_ms;
    this.cleanTurns = 0;
    this.player.port.postMessage({ type: "target", ms: this.bufferMs });
    if (this.fast) this.loadFillers();

    this.resampleCarry = new Float32Array(0);
    this.pcm16 = [];
    this.floorDb = -60;
    this.preroll = [];
    await this.connect(startMsg);
    if (this.cfg.asr_provider === "browser") this.startBrowserAsr();
    this.setState("listening");
  }

  async stop() {
    clearTimeout(this.refreshTimer);
    this.setState("off");
    try { this.recog?.abort(); } catch {}
    this.recog = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    try { await this.ctx?.close(); } catch {}
    this.ws?.close();
    this.ws = null;
  }

  get fast() {
    return this.cfg.latency_mode === "fast";
  }

  update(startMsg, cfg) {
    this.startMsg = startMsg; // a reconnect starts with the latest settings
    Object.assign(this.cfg, cfg);
    this.bufferMs = this.fast ? FAST_BUFFER.start : this.cfg.buffer_ms;
    this.player?.port.postMessage({ type: "target", ms: this.bufferMs });
    if (this.fast) this.loadFillers();
    if (this.ws?.readyState === 1) this.ws.send(JSON.stringify({ ...startMsg, type: "update" }));
    if (this.state !== "off") {
      if (this.cfg.asr_provider === "browser" && !this.recog) this.startBrowserAsr();
      if (this.cfg.asr_provider !== "browser" && this.recog) { try { this.recog.abort(); } catch {} this.recog = null; }
    }
  }

  connect(startMsg) {
    this.startMsg = startMsg;
    return new Promise((resolve, reject) => {
      const base = apiBase() || location.origin;
      const ws = new WebSocket(base.replace(/^http/, "ws") + "/ws/voice");
      const timer = setTimeout(() => (reject(new Error("Voice connection timed out.")), ws.close()), 15000);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(JSON.stringify(this.startMsg));
      ws.onmessage = (m) => {
        if (typeof m.data !== "string") return this.onAudio(m.data);
        const ev = JSON.parse(m.data);
        if (ev.type === "ready") (clearTimeout(timer), this.scheduleRefresh(), resolve());
        this.onServerEvent(ev);
      };
      ws.onerror = () => (clearTimeout(timer), reject(new Error("Voice connection failed. Check your connection and try again.")));
      ws.onclose = () => {
        clearTimeout(timer);
        if (this.ws !== ws) return; // an older socket we already replaced
        this.ws = null;
        if (this.state !== "off" && !this.reconnecting) this.reconnect();
      };
      this.ws = ws;
    });
  }

  // Audio frames are dropped while a socket is (re)connecting rather than throwing.
  sendAudio(buf) {
    if (this.ws?.readyState === 1) this.ws.send(buf);
  }

  // Vercel ends a voice socket after 5 minutes, and the next one may start a cold server instance
  // (a few seconds). Swap to a fresh socket a little early, and only while the agent is idle and
  // listening, so that wait never lands in the middle of a turn.
  scheduleRefresh() {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh(), REFRESH_AFTER_MS);
  }

  async refresh() {
    if (this.state === "off" || this.reconnecting) return;
    if (this.state !== "listening") {
      this.refreshTimer = setTimeout(() => this.refresh(), 2000); // mid-turn: try again shortly
      return;
    }
    const old = this.ws;
    try {
      await this.connect(this.startMsg); // replaces this.ws; the old socket's onclose then ignores it
      old?.close();
    } catch {
      this.refreshTimer = setTimeout(() => this.refresh(), 5000);
    }
  }

  // The server may close a long-lived socket (on Vercel, when the function reaches its maximum
  // duration). The conversation and cart live on the server, so a new socket carries on seamlessly;
  // at most the turn in flight is lost.
  async reconnect() {
    this.reconnecting = true;
    try {
      for (const wait of [200, 1000, 3000]) {
        await new Promise((r) => setTimeout(r, wait));
        if (this.state === "off") return;
        try {
          await this.connect(this.startMsg);
          if (this.state === "thinking") this.setState("listening");
          return;
        } catch {}
      }
      this.h.onError?.("Voice connection closed.");
      this.stop();
    } finally {
      this.reconnecting = false;
    }
  }

  setState(s) {
    this.state = s;
    this.h.onState?.(s);
  }

  // ------------------------------------------------------------------ microphone + endpointing

  onMicSamples(samples) {
    // Downsample the context rate (usually 48 kHz) to 16 kHz by linear interpolation.
    const ratio = this.ctx.sampleRate / 16000;
    const input = new Float32Array(this.resampleCarry.length + samples.length);
    input.set(this.resampleCarry);
    input.set(samples, this.resampleCarry.length);
    const n = Math.floor((input.length - 1) / ratio);
    for (let i = 0; i < n; i++) {
      const x = i * ratio, j = Math.floor(x), f = x - j;
      this.pcm16.push(input[j] * (1 - f) + input[j + 1] * f);
    }
    this.resampleCarry = input.slice(Math.floor(n * ratio));
    while (this.pcm16.length >= FRAME) this.onFrame(Float32Array.from(this.pcm16.splice(0, FRAME)));
  }

  onFrame(f) {
    let sum = 0;
    for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
    const db = 10 * Math.log10(sum / f.length + 1e-10);
    // Noise floor: drops instantly, rises slowly.
    this.floorDb = db < this.floorDb ? db : this.floorDb + Math.min(db - this.floorDb, FLOOR_RISE_DB);
    const voiced = db > Math.max(this.floorDb + START_MARGIN_DB, -55);
    this.h.onLevel?.(Math.max(0, Math.min(1, (db + 70) / 60)));
    const now = performance.now();
    const pcm = toPcm16(f);

    if (this.state === "thinking" || this.state === "speaking") {
      if (!this.cfg.barge_in) return;
      this.preroll.push(pcm);
      if (this.preroll.length > PREROLL_FRAMES) this.preroll.shift();
      const loud = db > Math.max(this.floorDb + BARGE_MARGIN_DB, -42);
      (this.bargeWin ||= []).push(loud);
      if (this.bargeWin.length > BARGE_WINDOW) this.bargeWin.shift();
      if (this.bargeWin.filter(Boolean).length >= BARGE_FRAMES) this.bargeIn(now);
      return;
    }
    if (this.state !== "listening" && this.state !== "hearing") return;
    if (this.state === "listening") {
      this.preroll.push(pcm);
      if (this.preroll.length > PREROLL_FRAMES) this.preroll.shift();
      this.voicedRun = voiced ? (this.voicedRun || 0) + 1 : 0;
      if (this.voicedRun >= START_FRAMES) this.beginUtterance(now, START_FRAMES);
      return;
    }
    // hearing
    if (this.cfg.asr_provider !== "browser") this.sendAudio(pcm.buffer);
    const silent = db < Math.max(this.floorDb + SILENCE_MARGIN_DB, this.speechDb - SPEECH_DROP_DB);
    if (!silent) {
      this.lastVoice = now;
      // The user's speaking level: rises quickly with louder speech, sinks slowly through softer words.
      this.speechDb += (db - this.speechDb) * (db > this.speechDb ? 0.3 : 0.02);
    }
    const silence = now - this.lastVoice;
    if (silence >= this.endpointMs() || now - this.speechStart > MAX_UTTERANCE_MS) this.onEndpoint(now, silence);
  }

  // Normal mode: the slider. Low-latency mode: short when the live transcript looks finished, long when
  // it trails off ("and…", "um"), in between when there is no transcript to judge by.
  endpointMs() {
    if (!this.fast) return this.cfg.endpoint_ms;
    const text = (this.lastPartial || "").trim();
    if (!text) return FAST_ENDPOINT.unknown;
    if (TRAILING.test(text)) return FAST_ENDPOINT.trailing;
    return /[.?!]$/.test(text) ? FAST_ENDPOINT.done : FAST_ENDPOINT.unknown;
  }

  beginUtterance(now, framesHeard) {
    this.setState("hearing");
    this.speechStart = now - framesHeard * 20;
    this.lastVoice = now;
    this.speechDb = this.floorDb + START_MARGIN_DB; // raised as the user's actual level is heard
    this.h.onPartial?.("");
    this.lastPartial = "";
    if (this.cfg.asr_provider !== "browser") {
      this.send({ type: "speech_start" });
      this.preroll.forEach((p) => this.sendAudio(p.buffer));
    }
    this.preroll = [];
    this.browserText = { final: "", interim: "" };
    this.voicedRun = 0;
    this.bargeWin = [];
  }

  bargeIn(now) {
    const turn = this.audioTurn;
    this.audioTurn = null; // drop any audio still in flight for the cancelled reply
    this.muteUntilNextUtterance = true; // ...including a reply whose tts_start is still on its way
    this.stopFiller();
    // Stop the sound now; the worklet answers with how much of this reply was actually played.
    this.player.port.postMessage({ type: "flush" });
    this.pendingBarge = { turn, at: now };
    this.lastServer = null;
    this.beginUtterance(now, BARGE_FRAMES);
  }

  onEndpoint(now, silence) {
    this.muteUntilNextUtterance = false;
    const spoke = this.lastVoice - this.speechStart;
    this.turn = { lastVoice: this.lastVoice, endpointAt: now, endpointMs: silence };
    if (spoke < MIN_SPEECH_MS) { // a cough or click, not speech
      if (this.cfg.asr_provider !== "browser") this.send({ type: "speech_end", endpoint_ms: silence });
      this.setState("listening");
      return;
    }
    this.setState("thinking");
    this.armFiller();
    if (this.cfg.asr_provider === "browser") this.finishBrowserAsr(now, silence);
    else this.send({ type: "speech_end", endpoint_ms: Math.round(silence) });
  }

  send(obj) {
    if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  // A typed message during a voice session: answered out loud like a spoken one. Latency is measured
  // from the moment it is sent (there is no end-of-speech wait).
  sendText(text) {
    const now = performance.now();
    this.turn = { lastVoice: now, endpointAt: now, endpointMs: 0 };
    this.muteUntilNextUtterance = false;
    this.setState("thinking");
    this.armFiller();
    this.send({ type: "text", text, asr_ms: 0, endpoint_ms: 0 });
  }

  // ------------------------------------------------------------------ browser ASR (Web Speech API)

  startBrowserAsr() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      this.h.onError?.("This browser has no Web Speech API (use Chrome or Edge), or pick a server ASR provider.");
      return;
    }
    const r = new SR();
    r.continuous = true;
    r.interimResults = true;
    r.lang = "en-US";
    this.browserText = { final: "", interim: "" };
    r.onresult = (e) => {
      if (this.state !== "hearing" && this.state !== "thinking") return;
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) this.browserText.final += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      this.browserText.interim = interim;
      this.lastPartial = (this.browserText.final + interim).trim();
      if (this.cfg.mode === "streaming") this.h.onPartial?.(this.lastPartial);
      this.browserFinalWaiter?.();
    };
    r.onend = () => { if (this.recog === r && this.state !== "off") try { r.start(); } catch {} }; // Chrome stops on its own
    r.onerror = (e) => { if (e.error === "not-allowed") this.h.onError?.("Microphone permission denied for speech recognition."); };
    this.recog = r;
    try { r.start(); } catch {}
  }

  finishBrowserAsr(endpointAt, silence) {
    // Wait briefly for the recognizer to finalize; fall back to its interim text.
    const done = () => {
      clearTimeout(timer);
      this.browserFinalWaiter = null;
      const text = (this.browserText.final + (this.browserText.final ? "" : this.browserText.interim)).trim();
      const asrMs = performance.now() - endpointAt;
      if (!text) {
        this.h.onEvent?.({ type: "asr_empty", ms: asrMs });
        this.setState("listening");
        return;
      }
      this.h.onEvent?.({ type: "asr_final", text, ms: asrMs, how: "browser" });
      this.send({ type: "text", text, asr_ms: Math.round(asrMs), endpoint_ms: Math.round(silence) });
    };
    const timer = setTimeout(done, 700);
    this.browserFinalWaiter = () => { if (!this.browserText.interim) done(); };
    if (this.browserText.final && !this.browserText.interim) done();
  }

  // ------------------------------------------------------------------ server events + playback

  onServerEvent(ev) {
    if (ev.type === "asr_partial") {
      this.lastPartial = ev.text;
      return this.h.onPartial?.(ev.text);
    }
    if (ev.type === "asr_empty") this.setState("listening");
    if (ev.type === "tts_start") {
      if (this.muteUntilNextUtterance) return; // the reply the user just talked over
      this.audioTurn = ev.turn;
      this.player.port.postMessage({ type: "reset" });
      Object.assign(this.turn || (this.turn = {}), { firstAudioAt: null, playStartAt: null, underruns: 0, bufferedAtStart: null });
    }
    if (ev.type === "tts_end") this.player.port.postMessage({ type: "end" });
    if (ev.type === "turn_done") {
      if (ev.turn !== this.audioTurn) return this.h.onEvent?.(ev); // reply was barged in on
      this.lastServer = ev;
      if (!this.turn?.firstAudioAt) this.finishTurn(); // nothing to play
    }
    if (ev.type === "error" && this.state === "thinking") this.setState("listening");
    this.h.onEvent?.(ev);
  }

  onAudio(buf) {
    const tid = new DataView(buf).getUint32(0, true);
    if (tid !== this.audioTurn) return; // late audio from a cancelled turn
    if (!this.turn.firstAudioAt) {
      this.turn.firstAudioAt = performance.now();
      clearTimeout(this.fillerTimer); // the reply is here: no filler needed
    }
    const pcm = new Int16Array(buf, 4);
    this.player.port.postMessage({ type: "chunk", samples: resampleTo(pcm, 24000, this.ctx.sampleRate) });
  }

  onPlayer(msg) {
    if (msg.type === "flushed") {
      const b = this.pendingBarge;
      this.pendingBarge = null;
      if (b) this.send({ type: "interrupt", turn: b.turn ?? null, played_ms: Math.round(msg.played_ms) });
      this.h.onEvent?.({ type: "barge_in", played_ms: msg.played_ms, stop_ms: performance.now() - (b ? b.at : performance.now()) });
      return;
    }
    // After a barge-in the worklet may still post about the reply it was playing; that reply is gone.
    if (this.audioTurn == null && ["started", "underrun", "done"].includes(msg.type)) return;
    if (msg.type === "started") {
      this.turn.playStartAt = performance.now();
      this.turn.bufferedAtStart = msg.buffered_ms;
      this.setState("speaking");
    } else if (msg.type === "underrun") {
      this.turn.underruns = msg.count;
    } else if (msg.type === "done") {
      this.turn.underruns = msg.underruns;
      if (this.state === "speaking" || this.state === "thinking") this.finishTurn();
    }
  }

  finishTurn() {
    const t = this.turn || {};
    const s = this.lastServer;
    if (s) {
      const c = {
        // client-side marks, all on performance.now()
        total: t.playStartAt ? t.playStartAt - t.lastVoice : null,             // user stops talking -> hears the reply
        filler: t.fillerAt ? t.fillerAt - t.lastVoice : null,                  // ...hears a filler (perceived)
        endpoint_wait: t.endpointMs,
        buffer_target: this.bufferMs,
        endpoint_to_first_byte: t.firstAudioAt ? t.firstAudioAt - t.endpointAt : null,
        buffer: t.playStartAt && t.firstAudioAt ? t.playStartAt - t.firstAudioAt : null,
        underruns: t.underruns || 0,
      };
      c.network = c.endpoint_to_first_byte != null && s.timings_ms.server_first_audio != null
        ? Math.max(0, c.endpoint_to_first_byte - s.timings_ms.server_first_audio) : null;
      this.h.onTurnDone?.({ ...s, client: c });
      this.adaptBuffer(c.underruns);
    }
    this.lastServer = null;
    clearTimeout(this.fillerTimer);
    if (this.state !== "off") this.setState("listening");
  }

  // ------------------------------------------------------------------ Low-latency mode helpers

  // Grow the buffer after a turn that stuttered, shrink it slowly after clean turns.
  adaptBuffer(underruns) {
    if (!this.fast) return;
    if (underruns > 0) {
      this.bufferMs = Math.min(FAST_BUFFER.max, this.bufferMs + FAST_BUFFER.step);
      this.cleanTurns = 0;
    } else if (++this.cleanTurns >= 3) {
      this.bufferMs = Math.max(FAST_BUFFER.min, this.bufferMs - 20);
      this.cleanTurns = 0;
    }
    this.player?.port.postMessage({ type: "target", ms: this.bufferMs });
  }

  // Synthesize the filler phrases once per voice and keep them in memory.
  async loadFillers() {
    const provider = this.cfg.tts_provider, voice = this.cfg.tts_voice || "";
    await Promise.all(FILLERS.map(async (text) => {
      const k = `${provider}|${voice}|${text}`;
      if (!provider || fillerCache.has(k)) return;
      fillerCache.set(k, null); // in flight
      try {
        const res = await fetch(apiBase() + "/api/voice/clip", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...keyHeaders() },
          body: JSON.stringify({ provider, voice: voice || null, text }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        fillerCache.set(k, new Int16Array(await res.arrayBuffer()));
      } catch {
        fillerCache.delete(k); // try again next session; fillers are optional
      }
    }));
  }

  armFiller() {
    clearTimeout(this.fillerTimer);
    if (this.fast) this.fillerTimer = setTimeout(() => this.playFiller(), FILLER_AFTER_MS);
  }

  // Play a cached filler on its own audio path. The reply buffer is held until the filler ends, so
  // the two never overlap and the reply's own latency is still measured honestly.
  playFiller() {
    if (this.state !== "thinking" || this.turn?.firstAudioAt) return;
    const provider = this.cfg.tts_provider, voice = this.cfg.tts_voice || "";
    const clips = FILLERS.map((t) => fillerCache.get(`${provider}|${voice}|${t}`)).filter(Boolean);
    if (!clips.length) return;
    const pcm = clips[Math.floor(Math.random() * clips.length)];
    const samples = resampleTo(pcm, 24000, this.ctx.sampleRate);
    const buf = this.ctx.createBuffer(1, samples.length, this.ctx.sampleRate);
    buf.copyToChannel(samples, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    this.player.port.postMessage({ type: "target", ms: 60000 }); // hold the reply until the filler ends
    src.onended = () => {
      if (this.filler === src) this.filler = null;
      this.player?.port.postMessage({ type: "target", ms: this.bufferMs });
    };
    this.filler = src;
    if (this.turn) this.turn.fillerAt = performance.now();
    src.start();
  }

  stopFiller() {
    clearTimeout(this.fillerTimer);
    if (this.filler) {
      try { this.filler.stop(); } catch {}
      this.filler = null;
      this.player?.port.postMessage({ type: "target", ms: this.bufferMs });
    }
  }
}

function toPcm16(f) {
  const out = new Int16Array(f.length);
  for (let i = 0; i < f.length; i++) out[i] = Math.max(-32768, Math.min(32767, Math.round(f[i] * 32767)));
  return out;
}

function resampleTo(pcm, from, to) {
  const ratio = from / to, n = Math.floor(pcm.length / ratio), out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = i * ratio, j = Math.floor(x), f = x - j;
    const a = pcm[j] / 32768, b = (j + 1 < pcm.length ? pcm[j + 1] : pcm[j]) / 32768;
    out[i] = a * (1 - f) + b * f;
  }
  return out;
}
