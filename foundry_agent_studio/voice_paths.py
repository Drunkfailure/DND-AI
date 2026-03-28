"""Resolve Piper models dir and Whisper GGML/GGUF: user config or default path under whisper_models/."""

from __future__ import annotations

from pathlib import Path

BUNDLED_WHISPER_MODEL_FILENAME = "ggml-small.bin"


def bundled_whisper_model_path() -> Path:
    return Path(__file__).resolve().parent / "whisper_models" / BUNDLED_WHISPER_MODEL_FILENAME


def effective_whisper_model_path(config_model_path: str) -> Path | None:
    """User path wins; if unset, use whisper_models/ggml-small.bin when that file exists (download separately)."""
    c = (config_model_path or "").strip()
    if c:
        p = Path(c)
        return p if p.is_file() else None
    b = bundled_whisper_model_path()
    return b if b.is_file() else None


def effective_whisper_model_path_str(config_model_path: str) -> str:
    p = effective_whisper_model_path(config_model_path)
    return str(p.resolve()) if p else ""


def bundled_piper_models_dir() -> Path:
    return Path(__file__).resolve().parent / "voices"


def effective_piper_models_dir(config_dir: str) -> Path | None:
    """Directory containing .onnx models. User path wins; if unset, use bundled voices when present."""
    c = (config_dir or "").strip()
    if c:
        p = Path(c)
        return p if p.is_dir() else None
    b = bundled_piper_models_dir()
    if b.is_dir() and any(b.glob("*.onnx")):
        return b
    return None


def effective_piper_models_dir_str(config_dir: str) -> str:
    p = effective_piper_models_dir(config_dir)
    return str(p.resolve()) if p else ""
