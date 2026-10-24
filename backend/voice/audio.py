"""PCM helpers. Mic audio arrives as 16 kHz mono PCM16; every TTS provider returns 24 kHz mono PCM16."""

from __future__ import annotations

import io
import wave

import numpy as np

MIC_RATE = 16_000
TTS_RATE = 24_000


def wav_bytes(pcm: bytes, rate: int = MIC_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    """Linear-interpolation resample of PCM16 mono (good enough for speech recognition)."""
    if src == dst or not pcm:
        return pcm
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    n = int(round(len(x) * dst / src))
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def duration_ms(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate * 1000
