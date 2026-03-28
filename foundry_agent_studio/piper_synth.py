"""Piper TTS: prefer `piper-tts` (PiperVoice); optional CLI fallback."""

from __future__ import annotations

import io
import wave
from pathlib import Path


def piper_tts_importable() -> bool:
    try:
        import piper  # noqa: F401

        return True
    except ImportError:
        return False


def synthesize_wav_piper_tts(model_path: Path, text: str) -> bytes:
    """Synthesize WAV bytes using `pip install piper-tts` (PiperVoice)."""
    from piper import PiperVoice

    if not model_path.is_file():
        raise FileNotFoundError(f"Piper model not found: {model_path}")
    voice = PiperVoice.load(str(model_path))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()
