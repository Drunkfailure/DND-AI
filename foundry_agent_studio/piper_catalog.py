"""Bundled Piper voice IDs (from `python -m piper.download_voices`)."""

from pathlib import Path

_CATALOG = Path(__file__).resolve().parent / "data" / "piper_voice_catalog.txt"


def load_piper_voice_ids() -> list[str]:
    raw = _CATALOG.read_text(encoding="utf-8")
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out
