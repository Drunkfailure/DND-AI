"""SQLite persistence — agents, short-term chat, long-term memory, app config."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from foundry_agent_studio.constants import PLAYER_SYSTEM_PREFIX


def utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Agent:
    id: str
    name: str
    description: str
    system_prompt: str
    model: str
    temperature: float
    memory_short_term_limit: int
    memory_long_term_enabled: bool
    embedding_model: str
    voice_provider: Optional[str]
    voice_model: Optional[str]
    stt_provider: Optional[str]
    voice_settings_json: str
    foundry_user_id: str
    foundry_actor_id: str
    foundry_world_id: str
    role: str
    knowledge_scope: str
    is_enabled: bool
    memory_stm_guidance: str
    memory_ltm_guidance: str
    memory_stm_filter: str
    memory_ltm_filter: str
    memory_ltm_agent_curated: bool
    world_wiki_url: str
    world_wiki_notes: str
    world_wiki_cached_text: str
    world_wiki_fetched_at: str
    world_wiki_cache_url: str
    foundry_sheet_snapshot: str
    created_at: str
    updated_at: str

    def full_system_prompt(self) -> str:
        return f"{PLAYER_SYSTEM_PREFIX}{self.system_prompt.strip()}"


def agent_to_row(a: Agent) -> dict[str, Any]:
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "systemPrompt": a.system_prompt,
        "model": a.model,
        "temperature": a.temperature,
        "memoryShortTermLimit": a.memory_short_term_limit,
        "memoryLongTermEnabled": a.memory_long_term_enabled,
        "embeddingModel": a.embedding_model,
        "voiceProvider": a.voice_provider,
        "voiceModel": a.voice_model,
        "sttProvider": a.stt_provider,
        "voiceSettingsJson": a.voice_settings_json,
        "foundryUserId": a.foundry_user_id,
        "foundryActorId": a.foundry_actor_id,
        "foundryWorldId": a.foundry_world_id,
        "role": a.role,
        "knowledgeScope": a.knowledge_scope,
        "isEnabled": a.is_enabled,
        "memoryStmGuidance": a.memory_stm_guidance,
        "memoryLtmGuidance": a.memory_ltm_guidance,
        "memoryStmFilter": a.memory_stm_filter,
        "memoryLtmFilter": a.memory_ltm_filter,
        "memoryLtmAgentCurated": a.memory_ltm_agent_curated,
        "worldWikiUrl": a.world_wiki_url,
        "worldWikiNotes": a.world_wiki_notes,
        "worldWikiFetchedAt": a.world_wiki_fetched_at,
        "foundrySheetSnapshot": a.foundry_sheet_snapshot,
        "createdAt": a.created_at,
        "updatedAt": a.updated_at,
    }


def _row_to_agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        system_prompt=row["system_prompt"],
        model=row["model"],
        temperature=row["temperature"],
        memory_short_term_limit=row["memory_short_term_limit"],
        memory_long_term_enabled=bool(row["memory_long_term_enabled"]),
        embedding_model=row["embedding_model"],
        voice_provider=row["voice_provider"],
        voice_model=row["voice_model"],
        stt_provider=row["stt_provider"],
        voice_settings_json=row["voice_settings_json"],
        foundry_user_id=row["foundry_user_id"],
        foundry_actor_id=row["foundry_actor_id"],
        foundry_world_id=row["foundry_world_id"],
        role=row["role"],
        knowledge_scope=row["knowledge_scope"],
        is_enabled=bool(row["is_enabled"]),
        memory_stm_guidance=str(row["memory_stm_guidance"] or ""),
        memory_ltm_guidance=str(row["memory_ltm_guidance"] or ""),
        memory_stm_filter=str(row["memory_stm_filter"] or ""),
        memory_ltm_filter=str(row["memory_ltm_filter"] or ""),
        memory_ltm_agent_curated=bool(row["memory_ltm_agent_curated"]),
        world_wiki_url=str(row["world_wiki_url"] or ""),
        world_wiki_notes=str(row["world_wiki_notes"] or ""),
        world_wiki_cached_text=str(row["world_wiki_cached_text"] or ""),
        world_wiki_fetched_at=str(row["world_wiki_fetched_at"] or ""),
        world_wiki_cache_url=str(row["world_wiki_cache_url"] or ""),
        foundry_sheet_snapshot=str(row["foundry_sheet_snapshot"] or ""),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _migrate_agents_memory_columns(conn: sqlite3.Connection) -> None:
    cols = [
        ("memory_stm_guidance", "TEXT NOT NULL DEFAULT ''"),
        ("memory_ltm_guidance", "TEXT NOT NULL DEFAULT ''"),
        ("memory_stm_filter", "TEXT NOT NULL DEFAULT ''"),
        ("memory_ltm_filter", "TEXT NOT NULL DEFAULT ''"),
        ("memory_ltm_agent_curated", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, decl in cols:
        try:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _migrate_agents_world_wiki(conn: sqlite3.Connection) -> None:
    for name, decl in [
        ("world_wiki_url", "TEXT NOT NULL DEFAULT ''"),
        ("world_wiki_notes", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def _migrate_agents_foundry_sheet(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN foundry_sheet_snapshot TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def _migrate_agents_wiki_web_cache(conn: sqlite3.Connection) -> None:
    for name, decl in [
        ("world_wiki_cached_text", "TEXT NOT NULL DEFAULT ''"),
        ("world_wiki_fetched_at", "TEXT NOT NULL DEFAULT ''"),
        ("world_wiki_cache_url", "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            system_prompt TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'llama3.2',
            temperature REAL NOT NULL DEFAULT 0.7,
            memory_short_term_limit INTEGER NOT NULL DEFAULT 20,
            memory_long_term_enabled INTEGER NOT NULL DEFAULT 1,
            embedding_model TEXT NOT NULL DEFAULT 'nomic-embed-text',
            voice_provider TEXT,
            voice_model TEXT,
            stt_provider TEXT,
            voice_settings_json TEXT NOT NULL DEFAULT '{}',
            foundry_user_id TEXT NOT NULL DEFAULT '',
            foundry_actor_id TEXT NOT NULL DEFAULT '',
            foundry_world_id TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'player',
            knowledge_scope TEXT NOT NULL DEFAULT 'character',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            memory_stm_guidance TEXT NOT NULL DEFAULT '',
            memory_ltm_guidance TEXT NOT NULL DEFAULT '',
            memory_stm_filter TEXT NOT NULL DEFAULT '',
            memory_ltm_filter TEXT NOT NULL DEFAULT '',
            memory_ltm_agent_curated INTEGER NOT NULL DEFAULT 0,
            world_wiki_url TEXT NOT NULL DEFAULT '',
            world_wiki_notes TEXT NOT NULL DEFAULT '',
            world_wiki_cached_text TEXT NOT NULL DEFAULT '',
            world_wiki_fetched_at TEXT NOT NULL DEFAULT '',
            world_wiki_cache_url TEXT NOT NULL DEFAULT '',
            foundry_sheet_snapshot TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS short_term_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_stm_agent ON short_term_messages(agent_id, created_at);

        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            content TEXT NOT NULL,
            embedding BLOB,
            created_at TEXT NOT NULL,
            FOREIGN KEY(agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_ltm_agent ON long_term_memory(agent_id);

        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    now = utc_now_rfc3339()
    conn.execute(
        "INSERT OR IGNORE INTO app_config (key, value) VALUES ('ollama_base', 'http://127.0.0.1:11434')"
    )
    conn.execute("INSERT OR IGNORE INTO app_config (key, value) VALUES ('bridge_port', '17890')")
    conn.execute(
        "INSERT OR IGNORE INTO app_config (key, value) VALUES ('bridge_secret', ?)",
        (str(uuid.uuid4()),),
    )
    for k, v in [
        ("piper_path", ""),
        ("piper_models_dir", ""),
        ("whisper_path", ""),
        ("whisper_model_path", ""),
    ]:
        conn.execute("INSERT OR IGNORE INTO app_config (key, value) VALUES (?, ?)", (k, v))
    _migrate_agents_memory_columns(conn)
    _migrate_agents_world_wiki(conn)
    _migrate_agents_foundry_sheet(conn)
    _migrate_agents_wiki_web_cache(conn)
    conn.commit()


def list_agents(conn: sqlite3.Connection) -> list[Agent]:
    cur = conn.execute(
        """SELECT id, name, description, system_prompt, model, temperature, memory_short_term_limit,
                memory_long_term_enabled, embedding_model, voice_provider, voice_model, stt_provider,
                voice_settings_json, foundry_user_id, foundry_actor_id, foundry_world_id,
                role, knowledge_scope, is_enabled,
                memory_stm_guidance, memory_ltm_guidance, memory_stm_filter, memory_ltm_filter,
                memory_ltm_agent_curated, world_wiki_url, world_wiki_notes,
                world_wiki_cached_text, world_wiki_fetched_at, world_wiki_cache_url, foundry_sheet_snapshot,
                created_at, updated_at
         FROM agents ORDER BY name"""
    )
    return [_row_to_agent(r) for r in cur.fetchall()]


def get_agent(conn: sqlite3.Connection, agent_id: str) -> Agent:
    cur = conn.execute(
        """SELECT id, name, description, system_prompt, model, temperature, memory_short_term_limit,
                memory_long_term_enabled, embedding_model, voice_provider, voice_model, stt_provider,
                voice_settings_json, foundry_user_id, foundry_actor_id, foundry_world_id,
                role, knowledge_scope, is_enabled,
                memory_stm_guidance, memory_ltm_guidance, memory_stm_filter, memory_ltm_filter,
                memory_ltm_agent_curated, world_wiki_url, world_wiki_notes,
                world_wiki_cached_text, world_wiki_fetched_at, world_wiki_cache_url, foundry_sheet_snapshot,
                created_at, updated_at
         FROM agents WHERE id = ?""",
        (agent_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError("not found")
    return _row_to_agent(row)


def insert_agent(conn: sqlite3.Connection, data: dict[str, Any]) -> Agent:
    aid = str(uuid.uuid4())
    now = utc_now_rfc3339()
    agent = Agent(
        id=aid,
        name=data["name"],
        description=data.get("description") or "",
        system_prompt=data.get("systemPrompt") or data.get("system_prompt") or "",
        model=data.get("model") or "llama3.2",
        temperature=float(data.get("temperature") if data.get("temperature") is not None else 0.7),
        memory_short_term_limit=int(data.get("memoryShortTermLimit") or data.get("memory_short_term_limit") or 20),
        memory_long_term_enabled=bool(
            data.get("memoryLongTermEnabled")
            if data.get("memoryLongTermEnabled") is not None
            else data.get("memory_long_term_enabled")
            if data.get("memory_long_term_enabled") is not None
            else True
        ),
        embedding_model=data.get("embeddingModel") or data.get("embedding_model") or "nomic-embed-text",
        voice_provider=(data.get("voiceProvider") or data.get("voice_provider")) or None,
        voice_model=(data.get("voiceModel") or data.get("voice_model")) or None,
        stt_provider=(data.get("sttProvider") or data.get("stt_provider")) or None,
        voice_settings_json=data.get("voiceSettingsJson") or data.get("voice_settings_json") or "{}",
        foundry_user_id=data.get("foundryUserId") or data.get("foundry_user_id") or "",
        foundry_actor_id=data.get("foundryActorId") or data.get("foundry_actor_id") or "",
        foundry_world_id=data.get("foundryWorldId") or data.get("foundry_world_id") or "",
        role=data.get("role") or "player",
        knowledge_scope=data.get("knowledgeScope") or data.get("knowledge_scope") or "character",
        is_enabled=bool(
            data.get("isEnabled") if data.get("isEnabled") is not None else data.get("is_enabled", True)
        ),
        memory_stm_guidance=data.get("memoryStmGuidance") or data.get("memory_stm_guidance") or "",
        memory_ltm_guidance=data.get("memoryLtmGuidance") or data.get("memory_ltm_guidance") or "",
        memory_stm_filter=data.get("memoryStmFilter") or data.get("memory_stm_filter") or "",
        memory_ltm_filter=data.get("memoryLtmFilter") or data.get("memory_ltm_filter") or "",
        memory_ltm_agent_curated=bool(
            data.get("memoryLtmAgentCurated")
            if data.get("memoryLtmAgentCurated") is not None
            else data.get("memory_ltm_agent_curated", False)
        ),
        world_wiki_url=data.get("worldWikiUrl") or data.get("world_wiki_url") or "",
        world_wiki_notes=data.get("worldWikiNotes") or data.get("world_wiki_notes") or "",
        world_wiki_cached_text="",
        world_wiki_fetched_at="",
        world_wiki_cache_url="",
        foundry_sheet_snapshot=data.get("foundrySheetSnapshot") or data.get("foundry_sheet_snapshot") or "",
        created_at=now,
        updated_at=now,
    )
    conn.execute(
        """INSERT INTO agents (
            id, name, description, system_prompt, model, temperature, memory_short_term_limit,
            memory_long_term_enabled, embedding_model, voice_provider, voice_model, stt_provider,
            voice_settings_json, foundry_user_id, foundry_actor_id, foundry_world_id,
            role, knowledge_scope, is_enabled,
            memory_stm_guidance, memory_ltm_guidance, memory_stm_filter, memory_ltm_filter,
            memory_ltm_agent_curated, world_wiki_url, world_wiki_notes,
            world_wiki_cached_text, world_wiki_fetched_at, world_wiki_cache_url, foundry_sheet_snapshot,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            agent.id,
            agent.name,
            agent.description,
            agent.system_prompt,
            agent.model,
            agent.temperature,
            agent.memory_short_term_limit,
            int(agent.memory_long_term_enabled),
            agent.embedding_model,
            agent.voice_provider,
            agent.voice_model,
            agent.stt_provider,
            agent.voice_settings_json,
            agent.foundry_user_id,
            agent.foundry_actor_id,
            agent.foundry_world_id,
            agent.role,
            agent.knowledge_scope,
            int(agent.is_enabled),
            agent.memory_stm_guidance,
            agent.memory_ltm_guidance,
            agent.memory_stm_filter,
            agent.memory_ltm_filter,
            int(agent.memory_ltm_agent_curated),
            agent.world_wiki_url,
            agent.world_wiki_notes,
            agent.world_wiki_cached_text,
            agent.world_wiki_fetched_at,
            agent.world_wiki_cache_url,
            agent.foundry_sheet_snapshot,
            agent.created_at,
            agent.updated_at,
        ),
    )
    conn.commit()
    return agent


def update_agent(conn: sqlite3.Connection, data: dict[str, Any]) -> Agent:
    aid = data.get("id")
    if not aid:
        raise ValueError("id required")
    cur = get_agent(conn, aid)
    if "name" in data:
        cur.name = data["name"]
    if "description" in data:
        cur.description = data["description"] or ""
    if "systemPrompt" in data or "system_prompt" in data:
        cur.system_prompt = data.get("systemPrompt") or data.get("system_prompt") or ""
    if "model" in data:
        cur.model = data["model"]
    if "temperature" in data and data["temperature"] is not None:
        cur.temperature = float(data["temperature"])
    if "memoryShortTermLimit" in data or "memory_short_term_limit" in data:
        v = data.get("memoryShortTermLimit", data.get("memory_short_term_limit"))
        if v is not None:
            cur.memory_short_term_limit = int(v)
    if "memoryLongTermEnabled" in data or "memory_long_term_enabled" in data:
        v = data.get("memoryLongTermEnabled")
        if v is None:
            v = data.get("memory_long_term_enabled")
        if v is not None:
            cur.memory_long_term_enabled = bool(v)
    if "embeddingModel" in data or "embedding_model" in data:
        cur.embedding_model = data.get("embeddingModel") or data.get("embedding_model") or cur.embedding_model
    if "voiceProvider" in data or "voice_provider" in data:
        v = data.get("voiceProvider", data.get("voice_provider"))
        cur.voice_provider = None if (v is None or v == "") else str(v)
    if "voiceModel" in data or "voice_model" in data:
        v = data.get("voiceModel", data.get("voice_model"))
        cur.voice_model = None if (v is None or v == "") else str(v)
    if "sttProvider" in data or "stt_provider" in data:
        v = data.get("sttProvider", data.get("stt_provider"))
        cur.stt_provider = None if (v is None or v == "") else str(v)
    if "voiceSettingsJson" in data or "voice_settings_json" in data:
        cur.voice_settings_json = data.get("voiceSettingsJson") or data.get("voice_settings_json") or "{}"
    if "foundryUserId" in data or "foundry_user_id" in data:
        cur.foundry_user_id = data.get("foundryUserId") or data.get("foundry_user_id") or ""
    if "foundryActorId" in data or "foundry_actor_id" in data:
        cur.foundry_actor_id = data.get("foundryActorId") or data.get("foundry_actor_id") or ""
    if "foundryWorldId" in data or "foundry_world_id" in data:
        cur.foundry_world_id = data.get("foundryWorldId") or data.get("foundry_world_id") or ""
    if "role" in data:
        cur.role = data["role"]
    if "knowledgeScope" in data or "knowledge_scope" in data:
        cur.knowledge_scope = data.get("knowledgeScope") or data.get("knowledge_scope") or cur.knowledge_scope
    if "isEnabled" in data or "is_enabled" in data:
        v = data.get("isEnabled")
        if v is None:
            v = data.get("is_enabled")
        if v is not None:
            cur.is_enabled = bool(v)
    if "memoryStmGuidance" in data or "memory_stm_guidance" in data:
        cur.memory_stm_guidance = data.get("memoryStmGuidance") or data.get("memory_stm_guidance") or ""
    if "memoryLtmGuidance" in data or "memory_ltm_guidance" in data:
        cur.memory_ltm_guidance = data.get("memoryLtmGuidance") or data.get("memory_ltm_guidance") or ""
    if "memoryStmFilter" in data or "memory_stm_filter" in data:
        cur.memory_stm_filter = data.get("memoryStmFilter") or data.get("memory_stm_filter") or ""
    if "memoryLtmFilter" in data or "memory_ltm_filter" in data:
        cur.memory_ltm_filter = data.get("memoryLtmFilter") or data.get("memory_ltm_filter") or ""
    if "memoryLtmAgentCurated" in data or "memory_ltm_agent_curated" in data:
        v = data.get("memoryLtmAgentCurated")
        if v is None:
            v = data.get("memory_ltm_agent_curated")
        if v is not None:
            cur.memory_ltm_agent_curated = bool(v)
    if "worldWikiUrl" in data or "world_wiki_url" in data:
        new_u = data.get("worldWikiUrl") or data.get("world_wiki_url") or ""
        if new_u.strip() != (cur.world_wiki_url or "").strip():
            cur.world_wiki_cached_text = ""
            cur.world_wiki_fetched_at = ""
            cur.world_wiki_cache_url = ""
        cur.world_wiki_url = new_u
    if "worldWikiNotes" in data or "world_wiki_notes" in data:
        cur.world_wiki_notes = data.get("worldWikiNotes") or data.get("world_wiki_notes") or ""
    if "foundrySheetSnapshot" in data or "foundry_sheet_snapshot" in data:
        cur.foundry_sheet_snapshot = data.get("foundrySheetSnapshot") or data.get("foundry_sheet_snapshot") or ""
    cur.updated_at = utc_now_rfc3339()
    conn.execute(
        """UPDATE agents SET
            name = ?, description = ?, system_prompt = ?, model = ?, temperature = ?,
            memory_short_term_limit = ?, memory_long_term_enabled = ?, embedding_model = ?,
            voice_provider = ?, voice_model = ?, stt_provider = ?, voice_settings_json = ?,
            foundry_user_id = ?, foundry_actor_id = ?, foundry_world_id = ?,
            role = ?, knowledge_scope = ?, is_enabled = ?,
            memory_stm_guidance = ?, memory_ltm_guidance = ?, memory_stm_filter = ?, memory_ltm_filter = ?,
            memory_ltm_agent_curated = ?, world_wiki_url = ?, world_wiki_notes = ?,
            world_wiki_cached_text = ?, world_wiki_fetched_at = ?, world_wiki_cache_url = ?, foundry_sheet_snapshot = ?,
            updated_at = ?
         WHERE id = ?""",
        (
            cur.name,
            cur.description,
            cur.system_prompt,
            cur.model,
            cur.temperature,
            cur.memory_short_term_limit,
            int(cur.memory_long_term_enabled),
            cur.embedding_model,
            cur.voice_provider,
            cur.voice_model,
            cur.stt_provider,
            cur.voice_settings_json,
            cur.foundry_user_id,
            cur.foundry_actor_id,
            cur.foundry_world_id,
            cur.role,
            cur.knowledge_scope,
            int(cur.is_enabled),
            cur.memory_stm_guidance,
            cur.memory_ltm_guidance,
            cur.memory_stm_filter,
            cur.memory_ltm_filter,
            int(cur.memory_ltm_agent_curated),
            cur.world_wiki_url,
            cur.world_wiki_notes,
            cur.world_wiki_cached_text,
            cur.world_wiki_fetched_at,
            cur.world_wiki_cache_url,
            cur.foundry_sheet_snapshot,
            cur.updated_at,
            cur.id,
        ),
    )
    conn.commit()
    return cur


def set_world_wiki_cache(conn: sqlite3.Connection, agent_id: str, cache_url: str, text: str) -> None:
    now = utc_now_rfc3339()
    conn.execute(
        """UPDATE agents SET world_wiki_cache_url = ?, world_wiki_cached_text = ?, world_wiki_fetched_at = ?,
            updated_at = ? WHERE id = ?""",
        (cache_url, text, now, now, agent_id),
    )
    conn.commit()


def find_responder_agent(
    conn: sqlite3.Connection,
    message_user_id: str,
    message_actor_id: str,
    world_id: str,
) -> Optional[Agent]:
    for a in list_agents(conn):
        if not a.is_enabled or a.role != "player":
            continue
        if a.foundry_world_id and world_id and a.foundry_world_id != world_id:
            continue
        linked_to_user = bool(a.foundry_user_id)
        linked_to_actor = bool(a.foundry_actor_id)
        if not linked_to_user and not linked_to_actor:
            continue
        if linked_to_user and a.foundry_user_id == message_user_id:
            continue
        if linked_to_actor and message_actor_id and a.foundry_actor_id == message_actor_id:
            continue
        return a
    return None


def find_agent_by_foundry_actor(
    conn: sqlite3.Connection, actor_id: str, world_id: str
) -> Optional[Agent]:
    """Agent whose linked Foundry actor id matches (for sheet sync)."""
    if not actor_id:
        return None
    for a in list_agents(conn):
        if a.foundry_actor_id != actor_id:
            continue
        if a.foundry_world_id and world_id and a.foundry_world_id != world_id:
            continue
        return a
    return None


def find_enabled_player_agent_by_actor_id(conn: sqlite3.Connection, actor_id: str) -> Optional[Agent]:
    """Single enabled player agent linked to this Foundry actor id (e.g. combat turn automation)."""
    if not actor_id:
        return None
    for a in list_agents(conn):
        if not a.is_enabled or a.role != "player":
            continue
        if a.foundry_actor_id != actor_id:
            continue
        return a
    return None


def list_linked_player_actor_ids(conn: sqlite3.Connection, _world_id: str = "") -> list[str]:
    """Foundry actor ids for enabled player agents (used by the module to auto-roll initiative).

    World id is ignored: a mismatched Foundry World ID in the app would otherwise drop
    every linked actor (common when the world slug/id changes). Combatants still match by actor id.
    """
    out: list[str] = []
    for a in list_agents(conn):
        if not a.is_enabled or a.role != "player":
            continue
        if not a.foundry_actor_id:
            continue
        out.append(a.foundry_actor_id)
    return out


def set_foundry_sheet_snapshot(conn: sqlite3.Connection, agent_id: str, snapshot_json: str) -> None:
    conn.execute(
        "UPDATE agents SET foundry_sheet_snapshot = ?, updated_at = ? WHERE id = ?",
        (snapshot_json, utc_now_rfc3339(), agent_id),
    )
    conn.commit()


def delete_agent(conn: sqlite3.Connection, agent_id: str) -> None:
    n = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,)).rowcount
    conn.commit()
    if n == 0:
        raise KeyError("not found")


def get_config(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def append_banter_line_all_agents(
    conn: sqlite3.Connection,
    speaker: Agent,
    spoken_line: str,
    agents: list[Agent],
) -> None:
    """Speaker gets an assistant line; everyone else gets a user line so all PCs 'hear' the banter."""
    line = spoken_line.strip()
    if not line:
        return
    user_line = f"Banter: {speaker.name}: {line}"
    for a in agents:
        append_short_term(
            conn,
            a.id,
            "assistant" if a.id == speaker.id else "user",
            line if a.id == speaker.id else user_line,
            a.memory_short_term_limit,
        )


def append_party_line_all_enabled(conn: sqlite3.Connection, text: str) -> int:
    """Append one shared user line to every enabled agent (everyone 'hears' the party)."""
    line = f"Party: {text}"
    n = 0
    for a in list_agents(conn):
        if not a.is_enabled:
            continue
        append_short_term(conn, a.id, "user", line, a.memory_short_term_limit)
        n += 1
    return n


def append_short_term(
    conn: sqlite3.Connection,
    agent_id: str,
    role: str,
    content: str,
    limit: int,
) -> None:
    now = utc_now_rfc3339()
    conn.execute(
        "INSERT INTO short_term_messages (agent_id, role, content, created_at) VALUES (?,?,?,?)",
        (agent_id, role, content, now),
    )
    conn.execute(
        """DELETE FROM short_term_messages WHERE agent_id = ? AND id NOT IN (
            SELECT id FROM short_term_messages WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?
        )""",
        (agent_id, agent_id, limit),
    )
    conn.commit()


def list_short_term(conn: sqlite3.Connection, agent_id: str, limit: int) -> list[tuple[str, str]]:
    cur = conn.execute(
        "SELECT role, content FROM short_term_messages WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
        (agent_id, limit),
    )
    rows = list(cur.fetchall())
    rows.reverse()
    return [(r[0], r[1]) for r in rows]


def long_term_content_exists(conn: sqlite3.Connection, agent_id: str, content: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM long_term_memory WHERE agent_id = ? AND content = ? LIMIT 1",
        (agent_id, content),
    )
    return cur.fetchone() is not None


def insert_long_term(
    conn: sqlite3.Connection,
    agent_id: str,
    kind: str,
    content: str,
    embedding: Optional[bytes],
) -> int:
    now = utc_now_rfc3339()
    cur = conn.execute(
        "INSERT INTO long_term_memory (agent_id, kind, content, embedding, created_at) VALUES (?,?,?,?,?)",
        (agent_id, kind, content, embedding, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_long_term_for_agent(
    conn: sqlite3.Connection, agent_id: str
) -> list[tuple[int, str, Optional[bytes]]]:
    cur = conn.execute(
        "SELECT id, content, embedding FROM long_term_memory WHERE agent_id = ? ORDER BY created_at DESC LIMIT 200",
        (agent_id,),
    )
    return [(int(r[0]), str(r[1]), r[2]) for r in cur.fetchall()]

