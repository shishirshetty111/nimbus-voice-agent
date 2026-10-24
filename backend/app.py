"""Nimbus voice-agent backend.

    ../.venv/Scripts/python -m uvicorn backend.app:app --port 8093

Serves the static site (whitelisted files only) and the API. API keys arrive per request in
`X-Api-Key-<provider>` headers from the browser's API-keys panel (localStorage). Locally, when a
header is absent, the server's .env key is used; in production (on Vercel) never - every visitor
brings their own keys. Server keys are never sent to the browser - only whether one exists.

On Vercel this app is the whole deployment: main.py at the repo root imports it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import chat, llm, prompts
from backend.answer import DEFAULT_MODEL, OPENAI_MODELS, stream_answer
from backend.catalog import LookupError_
from backend.config import PRODUCTION, ROOT, _KEY_ALIASES, api_key
from backend.context import load_context
from backend.llm.base import error_message
from backend.memory import memory
from backend.rag.build_index import is_current
from backend.rag.embedder import DEFAULT_EMBEDDER, EMBEDDERS, MissingKeyError
from backend.rag.retriever import DEFAULT_CANDIDATES, DEFAULT_K, DEFAULT_RERANKER, RERANKERS, Retriever
from backend.tools import registry as tools
from backend.tools.cart import store as cart_store
from backend.tools.registry import ToolContext
from backend.voice import asr as voice_asr
from backend.voice import tts as voice_tts
from backend.voice.session import DEFAULTS as VOICE_DEFAULTS, VoiceSession

log = logging.getLogger("nimbus")

# ------------------------------------------------------------------ retrievers

_retrievers: dict[str, Retriever] = {}
_retriever_lock = threading.Lock()
_warm = threading.Event()  # set once models are loaded and UMAP is compiled
_warm_started = threading.Event()


def get_retriever(embedder: str, key: str | None = None) -> Retriever:
    if embedder not in EMBEDDERS:
        raise HTTPException(400, f"unknown embedder {embedder!r}")
    with _retriever_lock:  # building an index is slow; never build the same one twice
        if embedder not in _retrievers:
            try:
                _retrievers[embedder] = Retriever(embedder, key=key)
            except MissingKeyError as e:
                raise HTTPException(401, str(e))
        return _retrievers[embedder]


def request_key(request: Request, provider: str) -> str | None:
    """Browser-supplied key wins; locally, fall back to the server .env key (never in production)."""
    return (request.headers.get(f"x-api-key-{provider}") or "").strip() or api_key(provider)


def _warm_up():
    try:
        load_context()
        get_retriever(DEFAULT_EMBEDDER).warm()
        _warm.set()
        log.warning("RAG ready: %s", DEFAULT_EMBEDDER)
    except Exception:  # keep serving the site even if warm-up fails
        log.exception("RAG warm-up failed")


def start_warm_up() -> None:
    """Load models + compile UMAP in the background so the server starts instantly. Runs once, from
    the startup hook or, if the host skips lifespan events, from the first request."""
    if not _warm_started.is_set():
        _warm_started.set()
        threading.Thread(target=_warm_up, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_warm_up()
    yield


app = FastAPI(title="Nimbus voice agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://nimbus-shishir.vercel.app", "http://localhost:8093", "http://127.0.0.1:8093",
                   *filter(None, os.getenv("EXTRA_CORS_ORIGINS", "").split(","))],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Make browsers re-check pages and scripts on every load (a cheap 304 when unchanged).
    Without this they may reuse an old ES module for hours, and one stale module whose exports
    no longer match leaves the page blank."""
    start_warm_up()
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# ------------------------------------------------------------------ API


@app.get("/api/health")
def health():
    return {"ok": True, "rag_ready": _warm.is_set(), "production": PRODUCTION}


@app.get("/api/keys/status")
def keys_status():
    """Which providers have a server-side key. Booleans only - never the key itself."""
    return {p: api_key(p) is not None for p in _KEY_ALIASES}


@app.get("/api/context/info")
def context_info():
    return load_context().info()


@app.get("/api/rag/info")
def rag_info():
    return {
        "defaults": {"embedder": DEFAULT_EMBEDDER, "k": DEFAULT_K, "candidates": DEFAULT_CANDIDATES,
                     "reranker": DEFAULT_RERANKER, "rerank": True},
        "embedders": [{"key": k, "label": label, "built": is_current(k), "loaded": k in _retrievers,
                       "needs_key": "openai" if k.startswith("openai") else None}
                      for k, (label, _) in EMBEDDERS.items()],
        "rerankers": [{"key": k, "model": m} for k, m in RERANKERS.items()],
        "manifest": _retrievers[DEFAULT_EMBEDDER].manifest if DEFAULT_EMBEDDER in _retrievers else None,
        "context": load_context().info(),
    }


@app.get("/api/rag/points")
def rag_points(request: Request, embedder: str = DEFAULT_EMBEDDER, dim: int = 2):
    if dim not in (2, 3):
        raise HTTPException(400, "dim must be 2 or 3")
    r = get_retriever(embedder, request_key(request, "openai"))
    xyz = r.coords[dim]
    return {
        "embedder": embedder,
        "manifest": r.manifest,
        "dim": dim,
        "points": [{"idx": i, "id": c["id"], "title": c["title"], "type": c["type"],
                    "product": c["product_name"], "category_id": c["category_id"],
                    "snippet": c["text"][len(c["title"]) + 1:][:220], "xyz": [float(v) for v in xyz[i]]}
                   for i, c in enumerate(r.chunks)],
    }


class QueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    k: int = Field(DEFAULT_K, ge=1, le=50)
    candidates: int = Field(DEFAULT_CANDIDATES, ge=1, le=100)
    rerank: bool = True
    reranker: str = DEFAULT_RERANKER
    embedder: str = DEFAULT_EMBEDDER
    dim: int = Field(2, ge=2, le=3)


@app.post("/api/rag/query")
def rag_query(body: QueryIn, request: Request):
    if body.reranker not in RERANKERS:
        raise HTTPException(400, f"unknown reranker {body.reranker!r}")
    key = request_key(request, "openai")
    r = get_retriever(body.embedder, key)
    try:
        res = r.retrieve(body.query, k=body.k, candidates=body.candidates, rerank=body.rerank,
                         reranker=body.reranker, key=key)
    except MissingKeyError as e:
        raise HTTPException(401, str(e))
    xyz, method = r.project(res.pop("query_vec"), body.dim, [h["idx"] for h in res["hits"]])
    res["query_xyz"], res["projection"] = xyz, method
    return res


class AnswerIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    mode: str = Field("rag", pattern="^(rag|ragless)$")
    model: str = DEFAULT_MODEL
    chunk_ids: list[str] = Field(default_factory=list, max_length=50)  # RAG: the hits already retrieved
    embedder: str = DEFAULT_EMBEDDER


@app.get("/api/llm/models")
def llm_models():
    return {"default": DEFAULT_MODEL,
            "models": [{"id": k, "label": v["label"]} for k, v in OPENAI_MODELS.items()]}


@app.post("/api/rag/answer")
def rag_answer(body: AnswerIn, request: Request):
    """Stream the answer as NDJSON: {"type":"delta"} ... then {"type":"done", timings, usage}."""
    if body.model not in OPENAI_MODELS:
        raise HTTPException(400, f"unknown model {body.model!r}")
    key = request_key(request, "openai")
    if not key:
        raise HTTPException(401, "No OpenAI API key: add one in the API keys panel.")
    chunks = []
    if body.mode == "rag":
        by_id = {c["id"]: c for c in get_retriever(body.embedder).chunks}
        chunks = [by_id[i] for i in body.chunk_ids if i in by_id]
        if not chunks:
            raise HTTPException(400, "Retrieve first: RAG mode answers from the retrieved chunks.")

    def events():
        try:
            for ev in stream_answer(key, body.model, body.mode, body.query, chunks):
                yield json.dumps(ev) + "\n"
        except Exception as e:  # surface auth/model errors to the UI instead of a broken stream
            err = getattr(e, "body", None)  # openai.APIStatusError carries the API's error object
            msg = (err.get("message") if isinstance(err, dict) else None) or getattr(e, "message", None) or str(e)
            yield json.dumps({"type": "error", "message": f"OpenAI error: {msg}"}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


# ------------------------------------------------------------------ cart + tools
# The cart id is a random id the browser keeps in localStorage and sends as X-Cart-Id.

CART_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def cart_id(request: Request) -> str:
    cid = request.headers.get("x-cart-id", "")
    if not CART_ID_RE.match(cid):
        raise HTTPException(400, "Missing or invalid X-Cart-Id header.")
    return cid


def tool_response(name: str, args: dict, request: Request) -> dict:
    ctx = ToolContext(cart_id=cart_id(request),
                      retrieve=lambda q, k=5: get_retriever(DEFAULT_EMBEDDER).retrieve(q, k=k, rerank=False))
    out = tools.call(name, args, ctx)
    if not out["ok"] and name in tools.BY_NAME:
        raise HTTPException(422, out["result"])
    return out


class AddIn(BaseModel):
    product: str
    tier: str | None = None
    seats: int = Field(1, ge=1, le=10000)
    billing: str = Field("annual", pattern="^(monthly|annual)$")


class LineIn(BaseModel):
    line_id: str | None = None
    product: str | None = None
    tier: str | None = None


@app.get("/api/cart")
def cart_get(request: Request):
    return cart_store.view(cart_id(request))


@app.post("/api/cart/add")
def cart_add(body: AddIn, request: Request):
    return tool_response("add_to_cart", body.model_dump(exclude_none=True), request)["result"]


@app.post("/api/cart/remove")
def cart_remove(body: LineIn, request: Request):
    try:
        return cart_store.remove(cart_id(request), body.line_id, body.product, body.tier)
    except LookupError_ as e:
        raise HTTPException(422, {"error": str(e)})


@app.post("/api/cart/clear")
def cart_clear(request: Request):
    return cart_store.clear(cart_id(request))


@app.post("/api/cart/checkout")
def cart_checkout(body: LineIn, request: Request):
    try:
        return cart_store.checkout(cart_id(request), body.line_id, body.product, body.tier)
    except LookupError_ as e:
        raise HTTPException(422, {"error": str(e)})


@app.get("/api/tools")
def tools_list():
    return {"tools": tools.describe()}


@app.post("/api/tools/{name}")
def tools_run(name: str, args: dict, request: Request):
    """Run one tool directly (the playground's tool tester). Errors come back in the result, as the LLM sees them."""
    if name not in tools.BY_NAME:
        raise HTTPException(404, f"unknown tool {name!r}")
    ctx = ToolContext(cart_id=cart_id(request),
                      retrieve=lambda q, k=5: get_retriever(DEFAULT_EMBEDDER).retrieve(q, k=k, rerank=False))
    return tools.call(name, args, ctx)


# ------------------------------------------------------------------ chat (text agent)


class ChatIn(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    message: str = Field(min_length=1, max_length=2000)
    settings: chat.ChatSettings = Field(default_factory=chat.ChatSettings)


class RulesIn(BaseModel):
    length: str = Field("medium", pattern="^(low|medium|high)$")
    knowledge_mode: str = Field("rag", pattern="^(rag|ragless)$")
    tools_enabled: list[str] | None = None
    latency_mode: str = Field("normal", pattern="^(normal|fast)$")


def _retrieve(q: str, k: int = 5, rerank: bool = False) -> dict:
    return get_retriever(DEFAULT_EMBEDDER).retrieve(q, k=k, rerank=rerank)


@app.get("/api/chat/config")
def chat_config():
    return {"providers": llm.catalog(), "default_provider": llm.DEFAULT_PROVIDER,
            "defaults": chat.ChatSettings().model_dump(), "default_persona": prompts.DEFAULT_PERSONA,
            "lengths": {k: v["label"] for k, v in prompts.LENGTHS.items()}}


@app.post("/api/chat/rules")
def chat_rules(body: RulesIn):
    settings = chat.effective(chat.ChatSettings(length=body.length, knowledge_mode=body.knowledge_mode,
                                                tools_enabled=body.tools_enabled, latency_mode=body.latency_mode))
    fast = settings.latency_mode == "fast"
    # Fast mode routes tools per message; preview the rules as they read when tools are offered.
    names = {t.name for t in chat.resolve(settings)[2]} - ({"search_knowledge"} if fast else set())
    return {"rules": prompts.rules_preview(settings.length, settings.knowledge_mode, names, fast=fast)}


@app.post("/api/chat")
def chat_turn(body: ChatIn, request: Request):
    """Stream one turn as NDJSON: rag?, delta*, tool*, round*, error?, then done (timings + memory)."""
    try:
        chat.resolve(body.settings)
    except ValueError as e:
        raise HTTPException(400, str(e))
    key = request_key(request, body.settings.provider)
    if not key:
        label = llm.PROVIDERS[body.settings.provider]["label"]
        raise HTTPException(401, f"No {label} API key: add one in the API keys panel.")
    cid = cart_id(request)

    def events():
        try:
            for ev in chat.run_turn(body.session_id, body.message, body.settings, key, cid, _retrieve):
                yield json.dumps(ev, default=str) + "\n"
        except Exception as e:  # never leave the browser with a half-open stream
            log.exception("chat turn failed")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/chat/{session_id}")
def chat_memory(session_id: str, verbatim: int = 6):
    return memory.get(session_id).snapshot(verbatim)


@app.post("/api/chat/{session_id}/reset")
def chat_reset(session_id: str):
    memory.reset(session_id)
    return {"ok": True}


# ------------------------------------------------------------------ voice


@app.get("/api/voice/config")
def voice_config():
    return {"asr": voice_asr.PROVIDERS, "defaults": VOICE_DEFAULTS,
            "tts": {k: {"label": v["label"], "model": v["model"], "voices": v["voices"], "default_voice": v["default_voice"]}
                    for k, v in voice_tts.PROVIDERS.items()}}


class ClipIn(BaseModel):
    provider: str
    voice: str | None = None
    text: str = Field(min_length=1, max_length=60)


@app.post("/api/voice/clip")
def voice_clip(body: ClipIn, request: Request):
    """Synthesize a short clip (a Low-latency mode filler like "One moment."), returned as raw 24 kHz
    PCM16. The browser caches it and plays it locally, so fillers cost nothing at turn time."""
    if body.provider not in voice_tts.PROVIDERS:
        raise HTTPException(400, f"unknown TTS provider {body.provider!r}")
    key = request_key(request, body.provider)
    if not key:
        raise HTTPException(401, f"No {voice_tts.PROVIDERS[body.provider]['label']} key.")
    voice = body.voice or voice_tts.PROVIDERS[body.provider]["default_voice"]
    try:
        pcm = voice_tts.batch(body.provider, body.text, voice, key)
    except Exception as e:
        raise HTTPException(502, error_message(e))
    return Response(content=pcm, media_type="audio/L16; rate=24000")


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    await ws.accept()
    await VoiceSession(ws, _retrieve).run()


# ------------------------------------------------------------------ static site
# Whitelist: top-level *.html pages, assets/ and data/. Nothing else in the repo is served.

app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
app.mount("/data", StaticFiles(directory=ROOT / "data"), name="data")


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/{page}")
def page(page: str):
    pages = {p.name for p in ROOT.glob("*.html")}  # re-read so new pages need no restart
    if page not in pages:
        raise HTTPException(404)
    return FileResponse(ROOT / page)
