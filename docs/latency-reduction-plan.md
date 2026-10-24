# Latency reduction plan: Nimbus voice agent

> **Build status (2026-10-10)**: Low-latency mode is implemented on the localhost site.
>
> **Built:**
> - **5.8 / 6.6:** connection reuse. `backend/clients.py` keeps one client per provider and key, in both modes.
> - **5.6:** auto-retrieval instead of the search tool, k = 3, no rerank.
> - **5.1 / 5.2 / 5.9:** lite model, low length, 4 history turns.
> - **5.11:** answer-first rule.
> - **5.12:** say-then-act rule.
> - **8.4:** per-message tool routing. The previous turn's tools carry over for one turn, so "yes, go ahead" can still check out.
> - **3.2:** adaptive end of speech: 500 ms ending in . ? ! / 700 ms otherwise / 1000 ms trailing. Raised from 300/450/700, then 450/600/900, after user testing: shorter waits cut people off mid-sentence. Pre-roll raised to 500 ms; ASR is pinned to English with a product-vocabulary prompt.
> - **4.4: tried and reverted.** Using the live partial after 300 ms garbled transcripts in user testing, because partials are uncorrected guesses. Fast mode now always waits for the final transcript.
> - **4.5:** pre-opened spare ASR socket.
> - **6.1:** first-clause chunking.
> - **6.7:** ElevenLabs `optimize_streaming_latency`.
> - **6.8:** two sentences synthesized ahead.
> - **7.2:** adaptive 40 ms buffer.
> - **6.4:** cached filler clips. They play on their own audio path after 700 ms with no reply; the reply's latency is still measured honestly.
> - **5.14:** parallel tool calls. OpenAI gets `parallel_tool_calls`, and fast mode prompts the model to batch independent lookups. A round's calls run concurrently unless one changes the cart (then in order). Tool time is now wall-clock per round.
> - **§9:** the switch, in the Playground Voice panel and on the Assistant page (default on there). The Latency dashboard has a "Normal vs ⚡ Low-latency" comparison and an A/B button.
>
> **Not built yet:**
> - **5.7:** speculative LLM start.
> - **5.13:** deterministic intent fast paths.
> - **3.3:** Silero VAD.
> - **7.1:** hosting near the providers.
> - **§11:** Realtime speech-to-speech mode.
>
> **Verified:** 70 tests pass. Checked in headless Chrome against the scripted test server: switch, rules preview, filler, adaptive end of speech, and the A/B comparison. **Real gains still need measuring with real API keys**: the scripted LLM ignores the profile.

**Goal:** about **2,000 ms** from *the user stops talking* to *the first audio they hear*, as shown in the Playground's Latency panel.

**How to read this:**

- ✅ means the code already supports it; it only needs choosing in the low-latency profile.
- 🟡 means it is partly there.
- 🆕 **MISSED** marks a step that was not in `latency-reduction.txt`.

Every step ends up behind **one switch: Low-latency mode** (section 9). Nothing changes for normal mode.

---

## 1. Where the time goes today

These are typical streaming-mode numbers on the current code. They are estimates; measure your own with the A/B runner.

| Stage | Today | Why |
|---|---|---|
| End of speech (silence wait) | 600–700 ms | Fixed slider, the same for every utterance |
| Speech recognition final | 150–700 ms | Streaming ASR waits for the provider's "completed" event. Batch re-uploads the whole clip |
| Retrieval | 10–15 ms, **or a whole extra LLM round** | `search_knowledge` is a *tool*, so knowledge questions cost model → tool → model again |
| LLM first token | 300–900 ms | A new HTTPS/TLS connection every turn; long system prompt plus 11 tool schemas on every request |
| Tools | < 10 ms each, **plus one LLM round each** | The tool code is fast. The extra model round trip is what costs |
| Wait for the first sentence | 200–600 ms | The splitter waits for `. ! ? ; :`. A long first sentence delays TTS |
| TTS first audio | 200–500 ms | A new connection *per sentence*; HTTP request setup each time |
| Network + buffer | 50–150 ms | 120 ms buffer default |
| **Total** | **≈ 1.6 – 4.5 s** | Knowledge and tool questions land at the top end |

---

## 2. Target budget (Low-latency mode)

| Stage | Budget | Main levers |
|---|---|---|
| End of speech | **250–350 ms** | Short and adaptive endpointing (§3.1, §3.2) |
| Speech recognition final | **50–150 ms** | Streaming, use the stable partial, pre-opened socket (§4) |
| Retrieval | **0–20 ms** | Prefetch during speech, no rerank, k = 3 (§5.3) |
| LLM first token | **250–450 ms** | Lite model, minimal reasoning, warm connection, cached short prompt (§5) |
| First speakable chunk | **50–150 ms** | First-clause chunking, "answer first" prompt (§6.1) |
| TTS first audio | **120–250 ms** | Flash or mini TTS, warm or WebSocket TTS (§6) |
| Network + buffer | **40–80 ms** | 40 ms adaptive buffer, server near the providers (§7) |
| **Total** | **≈ 0.8 – 1.5 s** typical, **≤ 2.0 s** with one tool call | |

Perceived latency can go **below** that with a filler acknowledgement (§6.4), but the dashboard should keep measuring the real reply.

---

## 3. Endpointing (end-of-speech)

| # | Step | Status | Expected gain |
|---|---|---|---|
| 3.1 | Reduce the endpoint silence to ~300 ms (your list says "300s"; this means **300 ms**) | ✅ Slider exists (200–1500 ms). The low-latency profile sets it | 300–400 ms |
| 3.2 | 🆕 **MISSED: adaptive / semantic endpointing.** Use a short wait (≈250 ms) when the live transcript already looks finished (ends in `?` or `.`, or is a complete question such as "what's the price of X"). Use a long wait (≈700 ms) when it ends mid-phrase ("and…", "um", "the"). A flat 300 ms cuts people off mid-sentence; this keeps 300 ms safe | 🆕 | 200–400 ms on average, without cut-offs |
| 3.3 | 🆕 **MISSED: a better speech detector.** Silero VAD (ONNX in the browser, about 2 MB) tells speech from noise more reliably than the energy detector. That makes a short endpoint safe in noisy rooms | 🆕 (energy VAD today) | Indirect: enables 3.1 and 3.2 |
| 3.4 | 🆕 **MISSED: count the end of speech from the last *voiced* frame,** not from the frame where silence is confirmed. This is already measured. Keep the pre-roll small so no time is spent re-sending audio | ✅ partly | 0–50 ms |

---

## 4. Speech recognition

| # | Step | Status | Expected gain |
|---|---|---|---|
| 4.1 | Browser ASR (Web Speech API) | ✅ Exists. Free and fast in Chrome, but no control over its endpointing and Chrome-only | Varies |
| 4.2 | Streaming mode | ✅ Exists for OpenAI, ElevenLabs and Gemini | 300–600 ms vs batch |
| 4.3 | Lite model | ✅ `gpt-live-transcribe`, Scribe realtime | — |
| 4.4 | 🆕 **MISSED: don't wait for the "final" event.** At the endpoint, use the latest **stable partial** transcript at once and send the final only to correct it if it differs. Today the server waits for `…transcription.completed` or `committed_transcript` after the commit | 🆕 | 100–400 ms |
| 4.5 | 🆕 **MISSED: open the streaming-ASR socket before the user speaks.** Today it opens at `speech_start`. Keep one per session open and reuse it across utterances, so the first words never wait for a TLS + WebSocket handshake | 🆕 | 100–300 ms on short utterances |
| 4.6 | 🆕 **MISSED: send audio in smaller chunks while hearing** (20 ms already) and never resample on the hot path more than once. OpenAI needs 24 kHz, so capture at 24 kHz for OpenAI in low-latency mode | 🟡 | 5–20 ms |

---

## 5. LLM

| # | Step | Status | Expected gain |
|---|---|---|---|
| 5.1 | Smaller model | ✅ Luna / Gemini Flash-Lite / Haiku 4.5 selectable | 200–600 ms |
| 5.2 | Restrict response length | ✅ "Low" length preset | Faster to the end, not to the first token |
| 5.3 | Fewer chunks (k) if RAG | ✅ Slider. Use k = 3 and **rerank off** (rerank costs 140–400 ms on this laptop) | 0–400 ms |
| 5.4 | RAG-less | ✅ Exists. Fastest only when the long prompt is **cached**. The first call is slow. See 5.8 | Varies |
| 5.5 | Streaming | ✅ Always on | — |
| 5.6 | 🆕 **MISSED: retrieval is a tool call today, costing a whole extra LLM round** (model → `search_knowledge` → model again, +300–800 ms). In low-latency mode, turn the **search tool off** and **auto-retrieve** instead. Better still, **prefetch** while the user is speaking from the partial transcript (§5.7), so the excerpts are ready at the endpoint | 🆕 (`chat.py` already has auto-retrieve when the tool is disabled) | **300–800 ms on knowledge questions** |
| 5.7 | 🆕 **MISSED: speculative start.** When the partial transcript has been stable for ~200 ms, start retrieval and even the LLM call before the endpoint fires. Cancel if the user keeps talking, keep the result if the final transcript matches. The cancellation plumbing exists from barge-in | 🆕 | 200–500 ms |
| 5.8 | 🆕 **MISSED: reuse connections (keep-alive).** `openai_llm.py`, `anthropic_llm.py` and `gemini_llm.py` create a **new SDK client every turn**, so every request pays DNS + TCP + TLS (often 100–300 ms). Cache one client per (provider, key) and reuse it. Optionally pre-warm with a tiny request when the mic turns on | 🆕 **(confirmed in code)** | 100–300 ms per turn |
| 5.9 | 🆕 **MISSED: make the prompt cacheable and small.** Keep the system prompt byte-for-byte stable (the summary is already appended last). Shorten the rules block for low-latency mode. Send **only the tools that turn needs**: 11 tool schemas are about 1.5k tokens per request. Fewer verbatim history turns (4 instead of 6) | 🟡 | 50–200 ms TTFT |
| 5.10 | 🆕 **MISSED: minimum reasoning/thinking** on every provider (OpenAI effort `none`, Gemini `thinking_level: low`, Claude effort `low`) | ✅ Already the defaults | — |
| 5.11 | 🆕 **MISSED: "answer first" instruction.** Tell the model to open with a short, direct sentence of five to eight words, for example "CRM Professional is $45 per user." Details follow. The first chunk then reaches TTS sooner | 🆕 | 100–300 ms |
| 5.12 | 🆕 **MISSED: say-then-act for tools.** Instruct the model to speak one short phrase *before* a tool call ("Let me add that for you.") and stream it to TTS immediately. The tool round then runs while the user is already hearing audio | 🆕 | Hides 300–800 ms on cart and pricing questions |
| 5.13 | 🆕 **MISSED: deterministic fast paths for the commonest intents.** Price, add-to-cart, cart-total and savings questions can be detected with simple patterns and answered by calling the tool **before** the LLM. The LLM then gets the tool result in the first request: one round instead of two | 🆕 (optional, larger change) | 300–700 ms on those intents |
| 5.14 | 🆕 **MISSED: parallel tool calls.** When several tools are needed (price + savings), allow the model to emit them in one round and run them concurrently | 🟡 (providers allow it; we run calls sequentially) | 0–300 ms |

---

## 6. Text-to-speech

| # | Step | Status | Expected gain |
|---|---|---|---|
| 6.1 | 🆕 **MISSED: first-clause chunking.** The splitter waits for sentence punctuation. For the *first* chunk, also cut at a comma or dash after ≥ 4 words, so TTS starts on "Sure, CRM Professional is $45," instead of the whole sentence | 🆕 | 100–400 ms |
| 6.2 | Lite / flash TTS models | ✅ `gpt-4o-mini-tts`, `eleven_flash_v2_5` | — |
| 6.3 | Streaming | ✅ Sentence-by-sentence streaming exists | — |
| 6.4 | Filler voice | 🆕 Your idea. Do it as **pre-synthesized clips cached in the browser** ("Sure.", "One moment.", "Let me check."), played **only** when the predicted wait is long (a tool call is expected, or nothing has arrived after 500 ms). Generating fillers live would cost the same latency it hides. Mark filler time separately in the dashboard so the real number stays honest | 🆕 | Perceived: −0.5 to −1.5 s |
| 6.5 | Buffer | ✅ Slider exists. Use **40 ms** in low-latency mode (§7.2) | 80 ms |
| 6.6 | 🆕 **MISSED: one warm TTS connection per session.** Today every sentence opens a new client and HTTP request. Reuse one `httpx` client and one OpenAI client per session. For ElevenLabs, use its **WebSocket input-streaming** API: send text as it arrives and receive audio continuously, with no per-sentence request setup | 🆕 **(confirmed in code)** | 100–250 ms on the first sentence, more on later ones |
| 6.7 | 🆕 **MISSED: request raw PCM at the playback rate.** PCM 24 kHz is already used (no MP3 decode). Keep the first TTS chunk small (ElevenLabs `optimize_streaming_latency` 3–4) | 🟡 | 50–150 ms |
| 6.8 | 🆕 **MISSED: synthesize sentence N+1 while N plays.** The TTS worker is sequential. Allow up to two TTS requests in flight so sentence 2 is ready when sentence 1 ends. This doesn't change first-audio latency, but removes gaps between sentences | 🆕 | No mid-reply pauses |

---

## 7. Network, buffer and hosting

| # | Step | Status | Expected gain |
|---|---|---|---|
| 7.1 | 🆕 **MISSED: host the backend near the providers** (US-East for OpenAI, ElevenLabs and Anthropic) and the users. Every provider round trip crosses this hop. From a laptop in India each call adds roughly 200–300 ms of round-trip time | 🆕 | 100–600 ms total |
| 7.2 | 🆕 **MISSED: adaptive jitter buffer.** Start at 40 ms; grow only if underruns occur during the session | 🆕 (fixed slider today) | 80 ms |
| 7.3 | 🆕 **MISSED: keep the server CPU free.** The reranker, UMAP and model warm-up must never run on the hot path; this laptop was memory- and CPU-starved. Turn the reranker off and warm everything at startup. Run on a machine with spare RAM | 🟡 | 0–500 ms (variance) |
| 7.4 | 🆕 **MISSED: avoid thread hops on the hot path.** Each turn crosses several queues and threads (LLM worker → TTS worker → asyncio queue → WebSocket). Fine at today's scale; profile once the bigger items are done | 🆕 | 5–30 ms |

---

## 8. Tools

| # | Step | Status | Expected gain |
|---|---|---|---|
| 8.1 | Minimize tokens in tool calls | 🟡 Return compact results (no full cart echo in every response, fewer fields). Shorter tool descriptions in low-latency mode | 20–100 ms TTFT on the next round |
| 8.2 | Make the implementation fast | ✅ Already < 10 ms per tool (measured). **Not the bottleneck: the extra LLM round per tool is** (see 5.6, 5.12, 5.13) | — |
| 8.3 | Less verbose | ✅ "Low" length preset plus tool results kept terse | — |
| 8.4 | 🆕 **MISSED: send only relevant tools per turn.** For example, no cart tools when the question is about policy. Fewer schemas means a smaller prompt and fewer chances the model calls an unneeded tool | 🆕 | 50–150 ms, and fewer accidental rounds |

---

## 9. The switch: Low-latency mode

**One toggle, everything in one profile.** Adding it means one constant on each side, not changes spread across the code.

### Where it lives
- **Backend:** `backend/latency_profile.py`, with `LOW_LATENCY = {...}` overrides applied to `ChatSettings` and the voice session settings when `latency_mode == "fast"`.
- **Frontend:** `assets/voice/latency-profile.js`. The Playground Voice panel gets a "⚡ Low-latency mode" toggle. The Assistant page and widget preset gets the same flag.
- **Each turn record** carries `profile: "normal" | "fast"`. The dashboard and A/B runner gain a **"Normal vs Low-latency"** comparison.

### What the profile sets

| Setting | Normal | Low-latency |
|---|---|---|
| Pipeline | user's choice | streaming |
| End of speech | 600–700 ms | **300 ms, adaptive 250–700 (3.2)** |
| ASR | user's choice | streaming, stable-partial (4.4), pre-opened socket (4.5) |
| LLM model | user's choice | lite per provider (Luna / Flash-Lite / Haiku 4.5) |
| Reasoning | minimum | minimum |
| Knowledge | RAG via tool | **auto-retrieve + prefetch** (5.6), k = 3, no rerank, *or* RAG-less if its prompt is cached |
| Reply length | medium | low + "answer first" (5.11) |
| History word for word | 6 | 4 |
| Tools | all | relevant subset (8.4); say-then-act (5.12) |
| First TTS chunk | first sentence | first clause (6.1) |
| TTS | user's choice | flash or mini, warm or WebSocket connection (6.6) |
| Playback buffer | 120 ms | 40 ms adaptive (7.2) |
| Filler clips | off | on, only when the wait is predicted to be long (6.4) |
| Speculative start | off | on (5.7) |
| Connection reuse | **always on** (no downside) | always on |

Connection reuse (5.8, 6.6) should apply to **both** modes. It's a pure win and keeps the comparison fair.

---

## 10. Order to implement (biggest win first)

1. **Connection reuse** for LLM and TTS clients (5.8, 6.6). Small change, saves 100–300 ms everywhere.
2. **Retrieval off the tool path** (5.6). Small, saves a whole LLM round on knowledge questions.
3. **Endpoint 300 ms + adaptive endpointing** (3.1, 3.2).
4. **First-clause chunking + "answer first" prompt** (6.1, 5.11).
5. **Stable-partial ASR + pre-opened socket** (4.4, 4.5).
6. **The Low-latency switch itself** (§9), plus the "Normal vs Low-latency" A/B comparison. Measure here.
7. **Say-then-act + filler clips** (5.12, 6.4).
8. **Speculative start + retrieval prefetch** (5.7).
9. **Deterministic fast paths** for top intents (5.13), if still above target.
10. **Hosting near the providers** (7.1) when deploying.

After each step, run the A/B runner (×3 rounds) and record the p50 and p95 totals in this file.

---

## 11. Beyond the cascade (if 2 s is still not enough)

🆕 **MISSED, an option outside the current architecture:** speech-to-speech models (OpenAI Realtime, Gemini Live native audio) take audio in and return audio out with **~500–800 ms** latency. They skip the ASR → LLM → TTS hand-offs. The trade-offs: less control over each stage, harder per-stage measurement, different tool and RAG wiring, and no choice of separate ASR/TTS vendors. Worth adding as a third mode ("Realtime") for comparison in the dashboard, rather than replacing the cascade.
