"""Text-to-speech. Every provider returns 24 kHz mono PCM16 so the browser player handles one format.

stream(...) yields audio as the provider produces it; batch(...) returns the whole clip at once.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator

from backend import clients

PROVIDERS = {
    "openai": {
        "label": "OpenAI", "model": "gpt-4o-mini-tts", "default_voice": "marin",
        "voices": ["marin", "cedar", "coral", "alloy", "ash", "ballad", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"],
    },
    "elevenlabs": {
        "label": "ElevenLabs", "model": "eleven_flash_v2_5", "default_voice": "JBFqnCBsd6RMkjVDRZzb",
        "voices": ["JBFqnCBsd6RMkjVDRZzb"],  # any voice id from the user's ElevenLabs library also works
    },
    "gemini": {
        "label": "Google Gemini", "model": "gemini-3.8-flash-tts", "default_voice": "Kore",
        "voices": ["Kore", "Puck", "Zephyr", "Fenrir", "Leda"],
    },
}
OPENAI_STYLE = "Speak like a friendly, upbeat customer-support agent on a phone call. Natural pace."
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}{suffix}"


def _openai(key: str):
    return clients.openai(key)


def stream(provider: str, text: str, voice: str, key: str, fast: bool = False) -> Iterator[bytes]:
    if provider == "openai":
        with _openai(key).audio.speech.with_streaming_response.create(
                model=PROVIDERS["openai"]["model"], voice=voice, input=text, instructions=OPENAI_STYLE,
                response_format="pcm") as r:
            yield from r.iter_bytes(4800)  # 100 ms at 24 kHz
    elif provider == "elevenlabs":
        params = {"output_format": "pcm_24000", **({"optimize_streaming_latency": 3} if fast else {})}
        with clients.http().stream("POST", ELEVEN_URL.format(voice=voice, suffix="/stream"), params=params,
                                   headers={"xi-api-key": key},
                                   json={"text": text, "model_id": PROVIDERS["elevenlabs"]["model"]}) as r:
            if r.status_code >= 400:
                r.read()
                raise RuntimeError(f"ElevenLabs TTS {r.status_code}: {r.text[:200]}")
            yield from r.iter_bytes(4800)
    elif provider == "gemini":
        yield from _gemini_stream(text, voice, key)
    else:
        raise ValueError(f"unknown TTS provider {provider!r}")


def batch(provider: str, text: str, voice: str, key: str) -> bytes:
    if provider == "openai":
        return _openai(key).audio.speech.create(model=PROVIDERS["openai"]["model"], voice=voice, input=text,
                                                instructions=OPENAI_STYLE, response_format="pcm").content
    if provider == "elevenlabs":
        r = clients.http().post(ELEVEN_URL.format(voice=voice, suffix=""), params={"output_format": "pcm_24000"},
                                headers={"xi-api-key": key},
                                json={"text": text, "model_id": PROVIDERS["elevenlabs"]["model"]}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs TTS {r.status_code}: {r.text[:200]}")
        return r.content
    if provider == "gemini":
        # The streamed form is raw PCM; collecting it all keeps batch semantics (play only when complete).
        return b"".join(_gemini_stream(text, voice, key))
    raise ValueError(f"unknown TTS provider {provider!r}")


def _gemini_stream(text: str, voice: str, key: str) -> Iterator[bytes]:
    events = clients.gemini(key).interactions.create(
        model=PROVIDERS["gemini"]["model"],
        input=[{"type": "user_input", "content": [{"type": "text", "text": text}]}],
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": voice}]},
        stream=True,
    )
    for ev in events:
        if getattr(ev, "event_type", None) == "error":
            raise RuntimeError(f"Gemini TTS: {getattr(ev, 'error', ev)}")
        data = getattr(getattr(ev, "delta", None), "data", None)
        if getattr(ev, "event_type", None) == "step.delta" and data:
            yield base64.b64decode(data)
