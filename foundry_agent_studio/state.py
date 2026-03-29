"""Shared application state."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    conn: sqlite3.Connection
    lock: threading.Lock
    outbox: list[dict[str, Any]] = field(default_factory=list)
    # Keys like "turn:actorId" / "chat:worldId:actorId" -> monotonic time; limits duplicate LLM/outbox bursts.
    bridge_event_debounce: dict[str, float] = field(default_factory=dict)
