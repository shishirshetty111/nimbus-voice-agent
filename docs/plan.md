# Nimbus Voice Agent — Implementation Plan

Source requirements: `../voice-agents-details.txt`.
Target: a configurable voice agent for the Nimbus site with a playground, a full-page agent, a landing-page
widget, a RAG visualizer, per-stage latency instrumentation, and (later) a public phone line.

---

## 0. Status of decisions

| # | Topic | Decision |
|---|-------|----------|
| D1 | Knowledge source | **`data/catalog.json`** is the single scrape source (no GitHub / rendered-HTML crawl). |
| D2 | Backend language | Python 3.11+, **FastAPI** + uvicorn (RAG must be Python; one WebSocket per session makes cancellation easy). |
| D3 | Vector store | **FAISS** `IndexFlatIP` over L2-normalized vectors (exact cosine; corpus is a few hundred chunks). |
| D4 | Embedding model | **`BAAI/bge-small-en-v1.5`** (384-d) via **fastembed** (ONNX, no torch, low RAM). Switchable to OpenAI `text-embedding-3-small` (1536-d) for comparison. |
| D5 | Reranker | **`Xenova/ms-marco-MiniLM-L-6-v2`** cross-encoder via fastembed (jina tiny/turbo selectable). Beat both jina models on the eval. |
| D6 | Top-k | Retrieve **top-10** from FAISS → rerank → keep **top-k = 5** (default). 20 candidates doubled rerank time for no recall gain. Both are UI sliders (k: 1–20, candidates: 5–50). |
| D10 | API keys | Entered in the browser's **API keys panel**, stored in `localStorage`, sent per request as `X-Api-Key-<provider>` headers. Server `.env` keys are a fallback and are never sent to the browser (`/api/keys/status` returns booleans only). |
| D7 | Cart | Moves **server-side**, keyed by session id; `cart.js` becomes a thin client. Required so web clicks, agent tools and phone calls share one cart. |
| D8 | Hosting | **Deferred** (local vs Railway). Build to run locally first; keep it Docker/Railway-ready. |
| D9 | Phone provider + auth | **Deferred.** Default assumption for design purposes: Twilio Media Streams + OTP. |

---

## 1. Architecture

```
Browser (static site + new pages)                FastAPI backend                         Providers
──────────────────────────────────               ───────────────                         ─────────
mic ─► VAD + endpointing (slider) ─► WS /ws/voice ─► ASR (batch | streaming) ───────────► OpenAI / Gemini / ElevenLabs
  (browser ASR runs client-side, sends text)        │                                     (Browser = Web Speech API)
                                                    ▼
                                         Context builder
                                           ├─ RAG on : search_knowledge tool / auto-retrieve (FAISS + rerank)
                                           └─ RAG off: context.md in system prompt (cached)
                                                    ▼
                                         LLM (+ enabled tools, memory) ─────────────────► OpenAI / Gemini / Anthropic
                                                    ▼  sentence chunks (streaming) / full text (batch)
speaker ◄─ AudioWorklet jitter buffer ◄─ WS ◄─ TTS (batch | streaming) ─────────────────► OpenAI / Gemini / ElevenLabs
latency panel ◄─ per-turn timeline JSON
cart drawer  ◄─ cart_updated events

Twilio call ─► WS /twilio/media ─► same pipeline (μ-law 8 kHz in/out)        [Phase 9]
```

The backend also serves the static site (`StaticFiles`) so everything is same-origin — no CORS, one deploy.

---

## 2. Repository layout

```
nimbus-voice-agent-starter/
├── plan.md
├── requirements.txt
├── Dockerfile, railway.json, .env.example          # Phase 9
├── scraper/
│   └── scrape.py                 # catalog.json → knowledge/*.md + context.md + chunks.jsonl
├── knowledge/                    # generated, committed
│   ├── company.md  products.md  families.md  pricing.md  monthly-vs-annual.md
│   ├── faq.md  refund.md  terms.md
│   ├── context.md                # all of the above, one file (RAG-less mode)
│   └── chunks.jsonl              # RAG chunks with metadata
├── backend/
│   ├── app.py                    # FastAPI: static, /ws/voice, /api/*
│   ├── config.py                 # env loading, model registry, defaults
│   ├── catalog.py                # typed access to catalog.json
│   ├── session.py                # per-connection state, settings, cancel tokens
│   ├── latency.py                # Timeline: mark()/span() → per-turn JSON
│   ├── memory.py                 # verbatim window + rolling summary
│   ├── prompts.py                # system prompt template, RAG policy, length presets
│   ├── rag/
│   │   ├── build_index.py        # chunks.jsonl → index.faiss + meta.json (+ umap model)
│   │   ├── embedder.py           # fastembed / OpenAI backends
│   │   ├── retriever.py          # retrieve(query, k, candidates, rerank) → hits + timings
│   │   └── viz.py                # 2D/3D projection of corpus + query
│   ├── tools/
│   │   ├── registry.py           # JSON schemas, enable/disable, dispatch, per-call timing
│   │   ├── cart.py               # cart store + cart tools
│   │   ├── pricing.py            # annual / savings / totals
│   │   └── catalog_tools.py      # sort, top-k expensive, search_knowledge
│   ├── asr/   base.py openai.py gemini.py elevenlabs.py
│   ├── llm/   base.py openai.py gemini.py anthropic.py
│   ├── tts/   base.py openai.py gemini.py elevenlabs.py
│   ├── pipeline.py               # turn orchestration, streaming vs batch, barge-in
│   └── phone/twilio.py           # Phase 9
├── assets/
│   ├── cart.js                   # modified: server-backed
│   └── voice/
│       ├── client.js             # WS protocol client, shared by all voice UIs
│       ├── mic.js  vad.js        # getUserMedia + Silero VAD (@ricky0123/vad-web)
│       ├── player.js  player-worklet.js   # jitter buffer + barge-in stop
│       ├── browser-asr.js        # Web Speech API adapter
│       ├── latency-chart.js      # stacked bar + donut
│       ├── playground.js  agent.js  widget.js  rag-viz.js
│       └── voice.css
├── playground.html               # every knob
├── agent.html                    # finalized agent, full page
├── rag.html                      # vector visualizer + query explorer
└── index.html                    # + floating chat widget
```

---

## 3. Configuration

`.env` (repo parent already has one; backend reads `../.env` then `./.env`):

| Var | Notes |
|---|---|
| `OPENAI_API_KEY` | present |
| `GEMINI_API_KEY` | currently named `GEMINI` — accept both |
| `ELEVENLABS_API_KEY` | currently named `ELEVENLABS` — accept both |
| `ANTHROPIC_API_KEY` | **missing — must be added** |
| `TWILIO_*` | Phase 9 |

API keys are entered in the browser's API keys panel (`assets/voice/keys.js`), stored in that browser's
`localStorage`, and sent with each request as `X-Api-Key-<provider>` headers (WebSocket: in `session.start`).
The server uses them for that request only, never logs or persists them, and falls back to `.env`.

Model registry (`config.py`, IDs verified against provider docs at build time):

| Provider | Lite | Heavy |
|---|---|---|
| OpenAI | small/mini chat model | flagship chat model |
| Gemini | Flash | Pro |
| Anthropic | `claude-haiku-4-5` | `claude-sonnet-5-5` (optional `claude-opus-5-5`) |

---

## 4. Phase 1 — Knowledge extraction (`scraper/scrape.py`)

Input: `data/catalog.json` (company, 8 policies, 8 categories, 24 products × 4 tiers, 96 product FAQs, add-ons).

Outputs in `knowledge/`:

| File | Content |
|---|---|
| `company.md` | name, tagline, founded, HQ, mission, stats, contact |
| `families.md` | each of the 8 categories → its products with one-line taglines |
| `products.md` | per product: summary, description, key features, specs, integrations, tiers, add-ons |
| `pricing.md` | table per product: tier, monthly price, annual-billed monthly price, unit, limits; note Enterprise = custom (null price) |
| `monthly-vs-annual.md` | for every paid tier: monthly, annual-billed monthly, yearly totals both ways, $ saved, % saved — **computed**, not written by an LLM |
| `faq.md` | all 96 FAQs grouped by product |
| `refund.md` | refund + free trial + cancellation policies |
| `terms.md` | billing, cancellation, SLA, security, data residency, support. Catalog has no explicit T&C; header states this is assembled from policies |
| `context.md` | all of the above concatenated with a table of contents (actual: ≈23.6k tokens, o200k_base) |
| `chunks.jsonl` | RAG chunks (below) |

Chunking (semantic, not fixed-size — the data is already structured):
- product overview (summary + description), product features+specs+integrations, **one chunk per tier**,
  add-ons, **one chunk per FAQ**, one chunk per policy, one per category, company profile.
- Each chunk: `{id, text, type, product_id, product_name, category_id, title}`; text prefixed with
  product/section title so it embeds well standalone. Actual: 282 chunks (24–203 tokens, avg 67).

Acceptance: script is idempotent, deterministic; prints file sizes, token estimate, chunk count.

---

## 5. Phase 2 — RAG

### 5.1 Index build (`rag/build_index.py`)
1. Load `chunks.jsonl`, embed (batch) with the selected embedder → normalize.
2. `faiss.IndexFlatIP(dim)`, add vectors, save `rag_store/<embedder>/index.faiss` + `meta.json`.
3. Fit UMAP (`n_neighbors=50, min_dist=0.5`, cosine — 15/0.1 left the templated FAQ groups as far islands that squashed the map) to 2D and 3D; save projected coords and the
   pickled reducer (for projecting queries via `.transform`). PCA fallback if UMAP unavailable.
4. Rebuild automatically at server start if `chunks.jsonl` hash changed.

### 5.2 Retrieval (`rag/retriever.py`)
```python
retrieve(query, k=5, candidates=20, rerank=True, filters=None) -> {
  hits: [{id, title, text, type, score_vec, score_rerank, rank_before, rank_after}],
  timings_ms: {embed, search, rerank, total}
}
```
- Optional metadata filter (e.g. product_id) applied post-search.
- Rerank toggle lets the UI show the latency/quality difference.

### 5.3 How the LLM uses RAG
- **RAG on + tools on:** RAG is exposed as tool `search_knowledge(query, k?)`. The system prompt defines:
  - **Use RAG for:** product features/specs/integrations, tier contents & limits, FAQs, refund/trial/
    cancellation/billing/SLA/security/data-residency policies, company info, comparisons between products.
  - **Do not use RAG for:** greetings/small talk, clarifying questions, cart operations, arithmetic on
    prices (use pricing tools), sorting/ranking products (use catalog tools), anything already answered
    in the recent conversation.
- **RAG on + tools off:** auto-retrieve on every user turn, inject top-k chunks into the prompt.
- **RAG off:** `context.md` placed in the system prompt (prefix-cached: Anthropic `cache_control`,
  OpenAI/Gemini automatic caching). No retrieval cost; larger prompt → higher LLM TTFT, which is measured.

### 5.4 Endpoints
- `GET /api/rag/points?dim=2|3&embedder=` → all chunk coords + metadata.
- `POST /api/rag/query {query, k, candidates, rerank}` → hits, timings, query coords, neighbor ids.
- `GET /api/rag/info` → embedder, dim, chunk count, k defaults.

### 5.5 Visualizer (`rag.html`)
- Plotly.js (cdnjs) scatter, 2D/3D toggle, color by category / chunk type, hover = title + snippet.
- Query box + **top-k slider** + candidates slider + rerank toggle.
- Query rendered as a star; lines to its top-k neighbors (opacity ∝ score); non-hits dimmed.
- Side panel: ranked table (vector score, rerank score, rank movement ↑↓), and timing bar
  (embed / search / rerank ms).
- Caveat shown in the UI: 2D/3D distances are a projection; true ranking is in 384-d space.

Acceptance: query "how do refunds work" returns the refund policy in top-3; visual shows clear category clusters.

**M2 results (done):** 30-question eval (`python -m backend.rag.eval`):

| Mode | R@1 | R@3 | R@5 | MRR | p50 latency (this laptop) |
|---|---|---|---|---|---|
| Vector only (bge-small) | 0.97 | 1.00 | 1.00 | 0.98 | ~10 ms |
| + MiniLM-L6 rerank, 10 candidates | 0.97 | 1.00 | 1.00 | 0.98 | ~140–400 ms (up to ~1 s under CPU load) |

On this small, clean corpus the reranker adds latency without measurable gain on the eval, so the
voice pipeline (M4+) should default **rerank off** and keep it as a toggle; the RAG Lab defaults it on to
show its effect. Eval set is hand-written and easy; revisit with harder/ambiguous questions.

### 5.6 RAGless vs FAISS RAG — the two knowledge modes

A single **Knowledge mode** switch in the playground: `RAGless (context.md)` | `FAISS RAG`.

| | RAGless | FAISS RAG |
|---|---|---|
| What the LLM sees | system prompt + **entire `context.md`** (≈23.6k tokens) | system prompt + **only top-k reranked chunks** (≈1–2k tokens) |
| Extra pipeline steps | none | embed query → FAISS search → rerank (or LLM round-trip to call `search_knowledge`) |
| Where latency goes | larger prompt → higher **LLM TTFT**; mitigated by prompt caching | **retrieval ms** + small prompt → lower TTFT; +1 LLM round-trip when used as a tool |
| Accuracy risk | "lost in the middle" on a long prompt | missed chunk if retrieval ranks it below k |
| Cost / turn | high input tokens (cheaper when cached) | low input tokens |

**RAGless details**
- `context.md` loaded once at startup, held in memory, placed at the **start** of the system prompt (stable
  prefix) so provider prompt caching applies: Anthropic `cache_control: ephemeral` on that block; OpenAI and
  Gemini cache matching prefixes automatically.
- Recorded per turn: `context_build` ms, prompt tokens, **cached tokens** (from provider usage), LLM TTFT.
  First turn (cold cache) vs later turns (warm) are flagged separately, because the difference is large.

**FAISS RAG details**
- Recorded per turn: `rag_embed`, `rag_search`, `rag_rerank`, `rag_total` ms, k used, chunk ids,
  prompt tokens, LLM TTFT, and whether retrieval came from the tool (+ the extra LLM round-trip ms) or auto-retrieve.

**How "added latency of RAG" is calculated**
```
rag_overhead_ms = (rag_total + extra_tool_roundtrip_ms + llm_ttft_rag)
                  − llm_ttft_ragless
```
i.e. what RAG costs end-to-end relative to RAGless for the same question. It can be **negative**: retrieval
adds ~20–60 ms locally, but a 30k-token prompt can add hundreds of ms of TTFT when the cache is cold.

**Comparison panel** (same pattern as streaming vs batch)
- Every turn record stores its knowledge mode. Panel shows per mode: mean/p50 total latency, mean LLM TTFT,
  mean retrieval ms, mean prompt tokens, cache-hit rate, and the computed `rag_overhead_ms`.
- **"Run in both modes"** button sends the last question through RAGless and FAISS RAG back-to-back (text
  path, same model) and shows the answers + latency breakdowns side by side.
- Offline harness (§14) runs the 30-question RAG eval in both modes → latency, tokens, and answer
  correctness for each, written to `reports/rag-vs-ragless.csv`.

---

## 6. Phase 3 — Tools

All deterministic Python over `catalog.py` and the cart store; each call timed.

| Tool | Args | Returns |
|---|---|---|
| `add_to_cart` | product, tier?, seats=1, billing=`monthly`\|`annual` | updated cart |
| `remove_from_cart` | product, tier? | updated cart |
| `clear_cart` | — | empty cart |
| `get_cart_total` | billing? | line items, monthly total, annual total |
| `checkout` | — | order id, total; cart cleared |
| `checkout_item` | product, tier? | order id for that one line; line removed |
| `annual_price` | product, tier, seats=1 | annual-billed monthly price, yearly total |
| `annual_savings` | product, tier, seats=1 | monthly×12 vs annual total, $ saved, % saved |
| `sort_products_by_price` | order=`asc`\|`desc`, tier?=lowest paid, category? | ordered list |
| `top_k_expensive` | k=5, tier?=highest priced, category? | top k products by price |
| `search_knowledge` | query, k? | RAG hits (only when RAG on) |

Rules:
- Product names fuzzy-matched (stdlib `difflib`; 'crm', 'nimbs crm', 'the payroll app' all resolve; tier prefixes like 'pro') → ambiguous matches return candidates
  so the LLM asks the user.
- Enterprise tiers (null price) are excluded from math and reported as "custom pricing — contact sales".
- Cart line keeps `billing` (the site's buttons add annual billing, as their "billed annually" labels say; the agent can add monthly too). Totals: per month, per year, and due today (annual lines charge 12 months up front).
- Registry exposes provider-neutral JSON Schema, translated per provider (OpenAI `tools`, Anthropic
  `tools`, Gemini `functionDeclarations`).
- Playground: checkbox per tool + **All** / **None**; only enabled schemas are sent to the LLM.
- Cart tools emit `cart_updated` over WS → cart drawer refreshes live.

Cart store: in-memory dict `{cart_id: [lines]}` persisted to `.runtime/carts.json` (SQLite later if needed). The browser's cart id is a random id in localStorage sent as `X-Cart-Id` (works cross-origin, unlike a cookie).

**M3 results (done):** 11 tools in `backend/tools/`, cart + tool HTTP endpoints, `cart.js` server-backed with localStorage fallback, Playground page with tool toggles (saved in `localStorage` as `nimbus_playground.tools_enabled`) and a tool tester. 34 tests pass. Tool calls take <10 ms (search_knowledge ~15 ms without rerank).
`cart_id` = session cookie (web) or verified phone number (phone). REST: `GET/POST/DELETE /api/cart/...`
used by the modified `cart.js`; falls back to localStorage if the backend is down (static hosting still works).

Unit tests: pricing math, savings %, sorting, top-k, fuzzy matching, cart ops.

---

## 7. Phase 4 — LLM reasoning layer (text-only first)

Provider adapters share one interface:
```python
async def stream(messages, system, tools, max_tokens, cancel) -> AsyncIterator[Delta]  # text | tool_call | done(usage)
```
Tool loop: stream → on tool calls, execute (timed), append results, continue; max 5 iterations.

Settings (from UI):
- **Provider + model** (lite/heavy per provider).
- **System prompt** — editable textarea, prefilled with default; the RAG usage policy (§5.3) and tool
  guidance are appended as a non-editable block (shown read-only) so toggles stay correct.
- **Response length:** low ≈ 1–2 sentences / 80 tokens, medium ≈ 3–4 sentences / 200, high ≈ detailed / 500.
  Implemented as both an instruction and `max_tokens`. Voice-style instruction: no markdown, no lists,
  spell prices naturally.
- **Memory:** slider *N verbatim turns* (0–20, default 6). Older turns are folded into a rolling summary by
  the lite model **after** the response is sent (off the critical path). Context sent =
  `system + [summary] + last N turns + current turn`.
- **Interruption fidelity:** on barge-in, the assistant message stored in history is truncated to the text
  actually spoken and suffixed with `[cancelled by the user]` (see §9).

Latency recorded: LLM TTFT, LLM total, each tool call, number of tool round-trips.

Acceptance: text chat in the playground works for all 3 providers with tools on/off and RAG on/off.

**M4 results (built; real-model runs pending user keys):**
- `backend/llm/{openai,anthropic,gemini}_llm.py`: streaming + native tool loop each, per-round TTFT/latency/tokens.
  Models (checked 2026-10-05): OpenAI `gpt-6-luna` (lite, default) / `gpt-6.1-sol` / `gpt-6-astra` (heavy, Responses API,
  lowest reasoning effort); Anthropic `claude-opus-5-5` (default) / `claude-sonnet-5-5` / `claude-haiku-4-5` (lite),
  effort `low`, server-side refusal fallback (`fallbacks: "default"`) on Opus/Sonnet 5.5, eager tool-input streaming
  with client-side validation; Gemini `gemini-3.8-flash` (default) / `gemini-3.5-flash-lite` / `gemini-3.1-pro-preview`,
  `thinking_level: low` (can't be disabled).
- RAG modes: search_knowledge as a tool (default), auto-retrieve when that tool is off, RAG-less puts context.md in the prompt.
- Memory (`backend/memory.py`): last N turns verbatim, older folded into a summary by the provider's lite model on a
  background thread; turns carry tool actions and the `[cancelled by the user]` flag (for M6).
- Prompt (`backend/prompts.py`): editable persona + fixed rules (voice style, length, RAG policy, tool safety).
- Playground: Agent brain settings + text chat with tool/RAG chips, per-turn latency, memory view. 43 tests pass
  (chat pipeline tested with a fake provider); all three providers verified to reach their APIs (auth errors).

---

## 8. Phase 5 — Voice: endpointing, ASR, TTS, streaming vs batch

### 8.1 Audio capture & endpointing (browser)
- `getUserMedia({echoCancellation, noiseSuppression, autoGainControl})`, 16 kHz mono PCM16 frames (20 ms).
- **Silero VAD** (`@ricky0123/vad-web`) for speech start/end.
- **Endpoint slider: 200–1500 ms** (default 700) = trailing silence required before the turn is final.
  This wait is reported as the `endpointing` latency stage.

### 8.2 ASR

| Provider | Batch | Streaming |
|---|---|---|
| Browser | Web Speech API final result | Web Speech interim + final |
| OpenAI | `gpt-4o-transcribe` on full utterance (WAV) | Realtime transcription WS, audio streamed while speaking |
| ElevenLabs | Scribe | Scribe realtime |
| Gemini | `generateContent` with inline audio | Live API (input transcription) |

- Browser ASR runs client-side; its latency is measured client-side (speech end → final) and sent with the text.
- Transcripts shown in the chat box; streaming mode shows live partials in grey.
- ASR latency = endpoint fired → final transcript available.

### 8.3 TTS

| Provider | Batch | Streaming |
|---|---|---|
| OpenAI | full text → audio file | `gpt-4o-mini-tts` streamed PCM, per sentence |
| ElevenLabs | full text → audio | WebSocket input streaming (Flash model), returns word alignment |
| Gemini | TTS model, full text | per-sentence requests |

- **Streaming mode:** LLM tokens → sentence splitter (punctuation + min length) → TTS per sentence → audio
  chunks streamed to browser immediately.
- **Batch mode:** full ASR → full LLM response → full TTS → play. Every stage waits for the previous one.

### 8.4 Playback buffer (`player-worklet.js`)
- AudioWorklet ring buffer; playback starts once **buffer target** (slider 0–500 ms, default 120) is filled.
- Displays: buffer target (ms added to first-audio latency), current fill, **underrun count** (audible gaps).
  Lets the user see the trade-off: 0 ms → lowest latency but choppy on jittery networks.

### 8.5 Streaming vs batch comparison
- Mode selector in playground; every turn record stores its mode.
- Comparison panel: per mode — turns, mean / p50 / p95 of total latency, mean per-stage breakdown,
  side-by-side bars. "Run same prompt in both modes" button replays the last utterance through both.

### 8.6 M5 as built (differs from the sketch above where noted)
- **Endpointing:** energy-based VAD in `assets/voice/voice-client.js` instead of Silero (no model download,
  no extra memory on this laptop): 20 ms frames, adaptive noise floor, speech = 15 dB above it, 300 ms pre-roll,
  250 ms minimum utterance, endpoint after the slider's silence (200–1500 ms, default 700).
  Fix (user report: turns never ended): the floor is now tracked all the time (drops instantly, rises 3 dB/s),
  and while the user talks a frame is silence once it's 18 dB below *their* speaking level or within 9 dB of
  the floor — so noise that rises after speech starts (mic auto-gain, a fan) can't hold the turn open.
  25 s hard cap per utterance. Reproduced and verified with a noisy-room clip (old rule: never ends).
- **ASR models (checked 2026-10-05):** OpenAI `gpt-transcribe` (batch) / `gpt-live-transcribe` over the Realtime
  WebSocket at 24 kHz (streaming); ElevenLabs `scribe_v2` / `scribe_v2_realtime` (manual commit);
  Gemini `gemini-3.5-flash-lite` with an audio part (batch) / `gemini-3.8-live` input transcription (streaming —
  Gemini has no transcription-only stream, so a Live session told to stay silent is used as a side channel).
- **TTS models:** OpenAI `gpt-4o-mini-tts` (PCM 24 kHz), ElevenLabs `eleven_flash_v2_5` HTTP streaming
  (`pcm_24000`), Gemini `gemini-3.8-flash-tts` via the Interactions API (streamed; batch collects the stream).
- **Pipeline:** `backend/voice/session.py`, one WebSocket per tab. Streaming = ASR while speaking, sentence
  splitter (first chunk released early), TTS worker thread speaking sentence N while the LLM writes N+1.
  Batch = whole utterance → whole reply → whole clip. A broken streaming-ASR socket falls back to batch
  for that utterance; the session survives.
- **Playback:** `assets/voice/worklets.js` jitter buffer (slider 0–500 ms), underrun counting.
- **Latency (per turn):** server durations (ASR, RAG, LLM TTFT, tools, TTS first byte, server first audio) +
  client marks on one clock (network, buffer, total = user stopped talking → audio starts). Full dashboard: M7.
- **Half-duplex** until M6: the mic is ignored while the agent thinks/speaks.
- Verified without keys: fake-mic headless Chrome reaches server ASR; each provider's ASR/TTS fails cleanly
  on a bad key and the session keeps working. Real-audio quality and latency need the user's keys.

---

## 9. Phase 6 — Barge-in (interrupt-driven conversation)

1. While the agent is speaking, VAD stays active. On speech start (with a ~150 ms min-speech guard to
   ignore coughs/echo), the browser **immediately** flushes the playback buffer and sends `interrupt`.
2. Server cancels the turn's cancel token → aborts LLM stream, pending tool loop, TTS requests.
3. Server computes text actually spoken:
   - ElevenLabs: from word-level alignment timestamps vs. played duration reported by the client.
   - Others: sentences fully played + proportional estimate of the current sentence (chars ∝ audio time).
4. History entry = spoken text + ` [cancelled by the user]`.
5. New user utterance proceeds as a normal turn.

Echo control: browser echo cancellation on; UI recommends headphones; optional "push-to-talk" fallback.

**M6 as built:**
- Detector while the agent thinks/speaks: 8 loud frames (+22 dB over the noise floor, > -42 dBFS) within any
  300 ms window. Windowed, not consecutive, because Chrome's echo cancellation chops the mic during playback.
- Client: flush the player worklet (reports ms actually played), send `interrupt{turn, played_ms}`, ignore the
  cancelled reply's late audio/tts_start, and treat the barge-in speech (with pre-roll) as the next utterance.
- Server: each utterance is one cancellable task (ASR → LLM/tools → TTS), so the receive loop always hears the
  interrupt. Heard text = sentences whose audio fully played + the proportional words of the cut one (unfinished
  sentences estimated at 14 chars/s). Memory is rewritten whether the LLM had finished (trim the stored reply)
  or not (store the partial); tool actions already taken are kept. Barge-in after the server finished but while
  the browser still played buffered audio is handled via the last turn's sentence→audio map. Barge-in during
  transcription drops that utterance.
- Proportional word estimate for all TTS providers (ElevenLabs alignment not used).
- Toggle in the Voice panel ("Let me talk over the agent"); interrupted replies show heard vs. struck-through text.
- Verified: 5 barge-in tests; headless Chrome + fake providers (`tests/fake_server.py`) for barge-in during
  thinking and during speech.

---

## 10. Phase 7 — Latency instrumentation & dashboard

Per-turn timeline (all ms, monotonic clocks; browser↔server offsets avoided by measuring each stage on
one side and sending durations, not timestamps):

| Stage | Measured where | Definition |
|---|---|---|
| `endpointing` | browser | last speech frame → endpoint fired (≈ slider value) |
| `asr` | server/browser | endpoint → final transcript |
| `rag_embed` / `rag_search` / `rag_rerank` | server | per retrieval |
| `context_build` | server | memory + prompt assembly (RAGless: incl. context.md block) |
| `rag_overhead` | server | derived, see §5.6 (shown in comparison panel, not summed into total) |
| `llm_ttft` | server | request → first token |
| `tools` | server | sum of tool executions (each listed) |
| `llm_total` | server | request → last token (informational) |
| `tts_ttfb` | server | first sentence sent → first audio byte |
| `network` | browser | server first-audio send → browser receive (via server-sent send-time delta) |
| `buffer` | browser | first chunk received → playback start |
| **`total`** | browser | **user stops speaking → first audio heard** |

Dashboard (`latency-chart.js`, plain SVG, no chart lib needed):
- Stacked horizontal bar per turn (stage colors consistent everywhere).
- Donut of percentage contribution for the current turn (and averaged), largest stage highlighted.
- Big number: total ms. Table of the last 20 turns, exportable as CSV.
- In streaming mode, stages overlap; the bar shows the **critical path** to first audio, overlaps noted.

**M7 as built (`assets/voice/dashboard.js`, Playground → Latency):**
- Critical-path stages that sum to the total: end of speech, ASR, RAG, LLM (think time minus RAG and tools),
  tools (excluding search retrieval), TTS first byte, network, buffer, and "other" for whatever is unexplained.
  Typed turns: RAG + tools + LLM to the first word.
- Part-to-whole as a 100% stacked bar + ranked table (largest stage highlighted) instead of a donut: the
  dataviz guidance puts donuts behind stacked bars for >6 parts. Stage colors are fixed (validated palette);
  low-contrast stages get dark labels; a table view and CSV export exist; hover tooltips on every segment.
- Comparisons: streaming vs batch (whole turn, voice + A/B turns); RAG vs RAG-less (retrieval + tools + LLM to
  the first sentence, streaming turns only so pipeline and metric match), each with mean/p50/p95 and a verdict.
- A/B runner: one question through both variants (×1–3 rounds), over /ws/voice with text in and audio out,
  a throwaway conversation and cart, timed to the first audio byte in the browser.
- History: last 200 turns in localStorage. Verified with the fake server (tests/fake_server.py adds realistic
  delays): streaming ≈ 0.82 s vs batch ≈ 1.47 s to first audio.

---

## 11. Phase 8 — Pages / UI

### `playground.html` — every option
- Providers: ASR, LLM (+ lite/heavy), TTS (+ voice).
- API keys: shared panel from `assets/voice/keys.js` (localStorage, masked with show/hide).
- Mode: streaming / batch. Endpoint slider. Buffer slider.
- Knowledge: RAG on/off, embedder, top-k, candidates, rerank toggle.
- Tools: checkbox list + All / None.
- System prompt editor, response length (low/med/high), memory verbatim-turns slider.
- Panels: chat transcript (with tool calls + retrieved chunks expandable), latency dashboard,
  mode comparison, cart mirror. Settings persisted per-viewer in localStorage; "reset to defaults".
- Text input box as an alternative to the mic (useful for debugging without audio).

### `agent.html` — finalized agent, full page
- Fixed best configuration (chosen after playground experiments), large mic button, live transcript,
  speaking indicator, cart panel, compact latency readout.

### Landing page widget (`index.html` + `widget.js`)
- Floating "Talk to Nimbus" bubble → chat panel with mic + text, same finalized config.
- Loaded via one `<script type="module">`; can be added to every page later.

All UIs use `assets/styles.css` tokens to match the existing design; add nav links to Playground / Agent / RAG.

**M8 as built:**
- Preset (`assets/voice/agent-core.js`): fastest model per provider (OpenAI gpt-6-luna → Gemini 3.8 Flash →
  Claude Haiku 4.5), RAG via the search tool, low reply length, 6 verbatim turns, all tools; streaming voice,
  600 ms endpoint, 120 ms buffer, barge-in on. ASR prefers OpenAI → ElevenLabs → browser → Gemini, TTS OpenAI
  (marin) → ElevenLabs → Gemini — each part uses the first provider with a key (browser or server). No speech
  key → text only. Dev hook: localStorage `nimbus_agent_override` overrides parts of the preset.
- `agent.html` (nav "Assistant"): big mic, live status/transcript, conversation with tool chips and barge-in
  display, typed input (spoken back when voice is on), live cart, "try saying" chips, last-reply latency bar.
- Landing widget (`assets/voice/widget.js`, mounted from index.html): floating "chat" button using the site's
  existing `.voice-fab` / `.voice-panel` styles; contacts the backend only when first opened.
- Agent page and widget share one conversation (`nimbus_agent_session`) and the site cart; their turns are
  stored in the same latency history the Playground dashboard reads (`assets/voice/latency-model.js`).
- Verified in headless Chrome against tests/fake_server.py: typed turn, cart panel, voice turn with barge-in,
  widget open + typed turn.

---

## 12. WebSocket protocol (`/ws/voice`)

Client → server:
```
session.start   {settings, api_keys?}
session.update  {settings}            # change any knob mid-session
audio.chunk     binary PCM16 16k      # server-side ASR modes
audio.end       {endpoint_ms, client_ts}
text.input      {text, asr_latency_ms?}   # browser ASR or typed input
interrupt       {played_ms}
playback.done   {played_ms, underruns, buffer_ms}
```
Server → client:
```
asr.partial / asr.final   {text}
llm.delta                 {text}
tool.call / tool.result   {name, args, result, ms}
rag.hits                  {hits, timings}
tts.audio                 binary PCM16 24k (tagged with sentence index)
turn.done                 {timeline}
cart_updated              {cart}
error                     {stage, message}
```

---

## 13. Phase 9 — Deployment & public phone line (decisions deferred)

- **Deploy:** Dockerfile (python-slim, fastembed model cache baked in), `railway.json`, healthcheck
  `/healthz`. Local run: `uvicorn backend.app:app --port 8093`.
- **Phone (assumed Twilio until decided):** number's voice webhook → TwiML `<Connect><Stream>` →
  `/twilio/media` WS. μ-law 8 kHz in; resample for ASR; TTS requested as `ulaw_8000` (ElevenLabs/OpenAI
  support it). Barge-in via Twilio `clear` message. Server-side VAD (Silero ONNX) for endpointing.
- **Phone auth (to be decided):** product info open to anyone; cart/checkout tools gated behind a verified
  identity — options: SMS OTP (Twilio Verify) or a 6-digit link code shown on the website that binds the
  caller to a web cart. Until verified, cart tools return "verification required".
- Requires a public HTTPS URL → only practical once hosting (D8) is decided.

---

## 14. Testing

- **Unit (pytest):** scraper outputs, chunker, retriever (golden queries → expected chunk in top-k),
  pricing/savings math, sorting, cart, memory summarization windowing, transcript truncation on interrupt.
- **Provider smoke tests:** one call per ASR/LLM/TTS adapter, skipped if key missing.
- **RAG eval:** ~30 hand-written Q→expected-chunk pairs; report recall@k and MRR with/without rerank.
- **Latency harness:** scripted run of N canned utterances (pre-recorded WAVs) through each mode/provider
  combo → CSV + summary, so streaming vs batch numbers are reproducible.
- **Manual:** barge-in, endpoint slider behaviour, buffer underruns under throttled network (DevTools).

---

## 15. Milestones

| # | Deliverable | Depends on |
|---|---|---|
| M1 | Scraper → `knowledge/*.md`, `context.md`, `chunks.jsonl` | — |
| M2 | FAISS index, retriever + rerank, `rag.html` visualizer, RAGless context loader | M1 |
| M3 | Tools + server-side cart + modified `cart.js` | — |
| M4 | LLM layer (3 providers), memory, prompts, text chat in playground | M2, M3 |
| M5 | ASR + TTS (4 + 3 providers), streaming & batch, buffer, endpointing | M4 |
| M6 | Barge-in + `[cancelled by the user]` history fidelity | M5 |
| M7 | Latency dashboard + streaming-vs-batch and RAG-vs-RAGless comparisons | M5 |
| M8 | `agent.html` + landing-page widget with finalized config | M6, M7 |
| M9 | Deployment + phone line + phone auth | D8, D9 |

---

## 16. Risks & notes

- **Anthropic key missing** from `.env` — Anthropic adapter untestable until added.
- **No real T&C in catalog** — `terms.md` is assembled from policies.
- **Browser ASR** only works in Chromium-based browsers (not Firefox).
- **Gemini ASR streaming** relies on the Live API; if its transcription-only use is awkward, Gemini
  streaming ASR may be approximated with chunked requests (will be labeled honestly in the UI).
- **Memory pressure** observed on the dev machine — reason for fastembed/ONNX over torch.
- **Cost:** heavy LLMs + TTS per turn; playground shows token usage per turn.
- Model IDs and provider API details are verified against current docs during implementation.

---

## 17. Python dependencies (initial)

`fastapi`, `uvicorn[standard]`, `python-dotenv`, `pydantic`, `httpx`, `websockets`,
`faiss-cpu`, `fastembed`, `numpy`, `umap-learn`,
`openai`, `anthropic`, `google-genai`, `elevenlabs`, `pytest`, `pytest-asyncio`
(+ `twilio`, `onnxruntime` for Phase 9).
