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
