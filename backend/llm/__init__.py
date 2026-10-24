"""LLM providers. Each module exposes MODELS, DEFAULT, LITE, run(TurnRequest) and complete()."""

from backend.llm import anthropic_llm, gemini_llm, openai_llm

PROVIDERS = {
    "openai": {"label": "OpenAI", "module": openai_llm},
    "anthropic": {"label": "Anthropic", "module": anthropic_llm},
    "gemini": {"label": "Google Gemini", "module": gemini_llm},
}
DEFAULT_PROVIDER = "openai"


def catalog() -> list[dict]:
    return [{"id": pid, "label": p["label"], "default": p["module"].DEFAULT,
             "models": [{"id": mid, **spec} for mid, spec in p["module"].MODELS.items()]}
            for pid, p in PROVIDERS.items()]
