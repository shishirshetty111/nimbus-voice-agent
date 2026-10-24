"""Paths, environment and API keys for the backend."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent          # nimbus-voice-agent-starter/
KNOWLEDGE_DIR = ROOT / "knowledge"
CHUNKS_PATH = KNOWLEDGE_DIR / "chunks.jsonl"
CONTEXT_PATH = KNOWLEDGE_DIR / "context.md"

# Production = deployed on Vercel (it sets VERCEL=1), or NIMBUS_ENV=production anywhere else.
PRODUCTION = bool(os.getenv("VERCEL")) or os.getenv("NIMBUS_ENV") == "production"

# On Vercel the code folder is read-only; /tmp is the only writable place.
RUNTIME_DIR = Path("/tmp/nimbus") if os.getenv("VERCEL") else ROOT
if os.getenv("VERCEL"):  # libraries that write caches next to themselves by default
    os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/nimbus/numba")      # UMAP's compiled functions
    os.environ.setdefault("HF_HOME", "/tmp/nimbus/hf")                 # model downloads, locks
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", "/tmp/nimbus/tiktoken")
SHIPPED_STORE_DIR = ROOT / "rag_store"                 # prebuilt FAISS indexes + projections (in git)
STORE_DIR = RUNTIME_DIR / "rag_store"                  # where an index is (re)built when needed
SHIPPED_MODEL_DIR = ROOT / ".cache" / "fastembed"      # ONNX models, bundled with a deployment if present
MODEL_CACHE_DIR = RUNTIME_DIR / ".cache" / "fastembed" if os.getenv("VERCEL") else SHIPPED_MODEL_DIR

# Keys live in the parent folder's .env; a local .env may override.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env", override=True)

# Accept the short names already used in ../.env as aliases.
_KEY_ALIASES = {
    "openai": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GEMINI"],
    "elevenlabs": ["ELEVENLABS_API_KEY", "ELEVENLABS"],
    "anthropic": ["ANTHROPIC_API_KEY"],
}


def api_key(provider: str) -> str | None:
    """The server's own key for a provider, from .env. Always None in production: there, every
    visitor brings their own keys, and the server must never spend the owner's credits on them
    (NIMBUS_ALLOW_SERVER_KEYS=1 overrides this for a private deployment)."""
    if PRODUCTION and os.getenv("NIMBUS_ALLOW_SERVER_KEYS") != "1":
        return None
    for var in _KEY_ALIASES[provider]:
        if os.getenv(var):
            return os.environ[var].strip()
    return None


def seed_model_cache() -> None:
    """On Vercel, copy the bundled ONNX models into /tmp once per cold start (about 150 MB, well
    under a second), because fastembed needs a writable cache folder. Missing models download."""
    import shutil

    if MODEL_CACHE_DIR == SHIPPED_MODEL_DIR or not SHIPPED_MODEL_DIR.is_dir() or MODEL_CACHE_DIR.is_dir():
        return
    shutil.copytree(SHIPPED_MODEL_DIR, MODEL_CACHE_DIR, dirs_exist_ok=True)
