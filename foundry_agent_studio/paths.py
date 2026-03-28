"""Application data directory and optional user-chosen storage folder (bootstrap file)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def default_app_data_dir() -> Path:
    """OS default app data root (bootstrap `paths.json` lives here)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "FoundryAgentStudio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FoundryAgentStudio"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "FoundryAgentStudio"
    return Path.home() / ".local" / "share" / "FoundryAgentStudio"


def bootstrap_paths_file() -> Path:
    """Small JSON next to the OS default folder; points at the real data directory."""
    return default_app_data_dir() / "paths.json"


def read_data_directory_override() -> Path | None:
    """If set, all app data (SQLite DB) lives under this folder."""
    p = bootstrap_paths_file()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = (data.get("dataDirectory") or data.get("data_directory") or "").strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_data_directory_override(path: str | None) -> None:
    """Write or clear override. Parent of bootstrap file is created."""
    bf = bootstrap_paths_file()
    bf.parent.mkdir(parents=True, exist_ok=True)
    if not path or not str(path).strip():
        if bf.is_file():
            bf.unlink()
        return
    data = {"dataDirectory": str(Path(path).expanduser().resolve())}
    bf.write_text(json.dumps(data, indent=2), encoding="utf-8")


def effective_data_directory() -> Path:
    o = read_data_directory_override()
    if o is not None:
        return o
    return default_app_data_dir()


def db_path() -> Path:
    """SQLite database path (respects `paths.json` data directory when set)."""
    return effective_data_directory() / "foundry_agent_studio.db"


# Backwards compatibility: older imports used `app_data_dir`
def app_data_dir() -> Path:
    return effective_data_directory()
