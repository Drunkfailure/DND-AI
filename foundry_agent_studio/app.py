"""FastAPI app: REST API, Ollama, Foundry bridge, static UI."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from foundry_agent_studio import db
from foundry_agent_studio.db import Agent, utc_now_rfc3339
from foundry_agent_studio.ollama_catalog import SUGGESTED_CHAT_MODELS, SUGGESTED_EMBEDDING_MODELS
from foundry_agent_studio.ollama_client import (
    bytes_to_f32_vec,
    chat_completion,
    chat_completion_stream_lines,
    cosine_similarity,
    embed_text,
    f32_vec_to_bytes,
    health,
    list_models,
    pull_model,
)
from foundry_agent_studio.bridge_rolls import format_sheet_snapshot_for_prompt, parse_fas_directives
from foundry_agent_studio.combat_context import (
    filter_mechanical_actions,
    format_combat_snapshot_for_prompt,
    get_combat_blob_from_conn,
    is_active_combat_snapshot,
)
from foundry_agent_studio.constants import BANTER_MODE_SUFFIX, FOUNDRY_ROLL_SYNTAX_HINT
from foundry_agent_studio.memory_gates import should_persist_stm_exchange, should_run_ltm_semantic
from foundry_agent_studio.paths import (
    bootstrap_paths_file,
    db_path,
    default_app_data_dir,
    effective_data_directory,
    read_data_directory_override,
    write_data_directory_override,
)
from foundry_agent_studio.voice_paths import (
    effective_piper_models_dir_str,
    effective_whisper_model_path_str,
)
from foundry_agent_studio.piper_catalog import load_piper_voice_ids
from foundry_agent_studio.piper_synth import piper_tts_importable, synthesize_wav_piper_tts
from foundry_agent_studio.state import AppState
from foundry_agent_studio.web_fetch import MAX_PROMPT_WIKI_CHARS, fetch_url_text
from foundry_agent_studio.voice_binaries import (
    is_wav_header,
    resolve_sidecar_exe,
    resolve_whisper_cli_exe,
    run_piper_to_wav,
    run_whisper_cli_to_text,
    write_temp_wav,
)


def _get_ollama_base(state: AppState) -> str:
    with state.lock:
        v = db.get_config(state.conn, "ollama_base")
    return v or "http://127.0.0.1:11434"


PARTY_REPLY_PROMPT = (
    "Respond in character to what was just said to the whole party (see the Party: line above). "
    "Speak only as your PC; keep it concise."
)

WIKI_CACHE_TTL_SECONDS = 6 * 3600


def _wiki_cache_needs_refresh(agent: Agent) -> bool:
    url = (agent.world_wiki_url or "").strip()
    if not url:
        return False
    if (agent.world_wiki_cache_url or "").strip() != url:
        return True
    if not (agent.world_wiki_cached_text or "").strip():
        return True
    ts = (agent.world_wiki_fetched_at or "").strip()
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age > WIKI_CACHE_TTL_SECONDS
    except ValueError:
        return True


async def refresh_agent_wiki_cache_if_needed(
    st: AppState, agent: Agent, *, force: bool = False
) -> Agent:
    """Fetch world wiki URL server-side when stale; cache in SQLite (not exposed in full to API)."""
    if not force and not _wiki_cache_needs_refresh(agent):
        return agent
    url = (agent.world_wiki_url or "").strip()
    if not url:
        return agent
    try:
        text = await asyncio.to_thread(fetch_url_text, url)
    except Exception as e:
        text = f"[Wiki page fetch failed: {e}]"

    def save() -> None:
        with st.lock:
            db.set_world_wiki_cache(st.conn, agent.id, url, text)

    await asyncio.to_thread(save)
    with st.lock:
        return db.get_agent(st.conn, agent.id)


def _append_world_wiki_parts(parts: list[str], agent: Agent) -> None:
    u = (agent.world_wiki_url or "").strip()
    n = (agent.world_wiki_notes or "").strip()
    cached = (agent.world_wiki_cached_text or "").strip()
    fetched_at = (agent.world_wiki_fetched_at or "").strip()
    if u:
        parts.append(
            "Campaign world wiki: the app fetched this public page for you (cached snapshot; not live browsing).\n"
            f"URL: {u}"
        )
        if fetched_at:
            parts.append(f"Snapshot fetched at: {fetched_at}")
        if cached:
            excerpt = cached[:MAX_PROMPT_WIKI_CHARS]
            if len(cached) > MAX_PROMPT_WIKI_CHARS:
                excerpt = excerpt + "\n… (truncated for prompt size)"
            parts.append("Fetched page text (HTML removed; use for lore consistency):\n" + excerpt)
        else:
            parts.append(
                "(No cached text yet — it will populate on the next message after save, or if the URL is unreachable.)"
            )
    if n:
        parts.append(
            "World / campaign reference (wiki excerpt or GM notes; not in-character dialogue):\n" + n
        )


def _append_foundry_sheet_and_roll_hint(parts: list[str], agent: Agent) -> None:
    if (agent.foundry_actor_id or "").strip():
        parts.append(FOUNDRY_ROLL_SYNTAX_HINT.strip())
    snap = (agent.foundry_sheet_snapshot or "").strip()
    if snap:
        formatted = format_sheet_snapshot_for_prompt(snap)
        if formatted:
            parts.append(
                "Character sheet snapshot (synced from Foundry VTT; use for HP, AC, gear, abilities):\n"
                + formatted
            )


def _append_combat_context(parts: list[str], state: AppState, agent: Agent) -> None:
    if not (agent.foundry_actor_id or "").strip():
        return
    block = format_combat_snapshot_for_prompt(state.conn, agent)
    if block:
        parts.append(block)


def _build_messages(state: AppState, agent: Agent, latest: str) -> list[tuple[str, str]]:
    with state.lock:
        system = agent.full_system_prompt()
        parts = [system]
        if (agent.memory_stm_guidance or "").strip():
            parts.append(
                "Guidance for recent dialogue (short-term context):\n" + agent.memory_stm_guidance.strip()
            )
        if (agent.memory_ltm_guidance or "").strip():
            parts.append(
                "Guidance for persistent character memory:\n" + agent.memory_ltm_guidance.strip()
            )
        _append_world_wiki_parts(parts, agent)
        _append_foundry_sheet_and_roll_hint(parts, agent)
        _append_combat_context(parts, state, agent)
        ltm = db.list_long_term_for_agent(state.conn, agent.id)
        if ltm:
            buf = "Character memory (facts the PC would recall):\n"
            for _, text, _ in ltm[:24]:
                buf += f"- {text}\n"
            parts.append(buf)
        merged = "\n\n".join(parts)
        msgs: list[tuple[str, str]] = [("system", merged)]
        short = db.list_short_term(state.conn, agent.id, agent.memory_short_term_limit)
        for role, content in short:
            if role in ("user", "assistant"):
                msgs.append((role, content))
        msgs.append(("user", latest))
    return msgs


def _build_messages_party_reply(state: AppState, agent: Agent) -> list[tuple[str, str]]:
    """After a Party: line was broadcast to all agents, ask this agent to respond in character."""
    with state.lock:
        system = agent.full_system_prompt()
        parts = [system]
        if (agent.memory_stm_guidance or "").strip():
            parts.append(
                "Guidance for recent dialogue (short-term context):\n" + agent.memory_stm_guidance.strip()
            )
        if (agent.memory_ltm_guidance or "").strip():
            parts.append(
                "Guidance for persistent character memory:\n" + agent.memory_ltm_guidance.strip()
            )
        _append_world_wiki_parts(parts, agent)
        _append_foundry_sheet_and_roll_hint(parts, agent)
        _append_combat_context(parts, state, agent)
        ltm = db.list_long_term_for_agent(state.conn, agent.id)
        if ltm:
            buf = "Character memory (facts the PC would recall):\n"
            for _, text, _ in ltm[:24]:
                buf += f"- {text}\n"
            parts.append(buf)
        merged = "\n\n".join(parts)
        msgs: list[tuple[str, str]] = [("system", merged)]
        short = db.list_short_term(state.conn, agent.id, agent.memory_short_term_limit)
        for role, content in short:
            if role in ("user", "assistant"):
                msgs.append((role, content))
        msgs.append(("user", PARTY_REPLY_PROMPT))
    return msgs


def _build_messages_banter(
    state: AppState,
    agent: Agent,
    peer_names: str,
    topic: str,
    turn_index: int,
    prev_speaker_name: Optional[str],
) -> list[tuple[str, str]]:
    """Short out-of-combat banter between PCs; capped turns at the API layer."""
    with state.lock:
        system = agent.full_system_prompt()
        parts = [system, BANTER_MODE_SUFFIX.strip()]
        if (agent.memory_stm_guidance or "").strip():
            parts.append(
                "Guidance for recent dialogue (short-term context):\n" + agent.memory_stm_guidance.strip()
            )
        if (agent.memory_ltm_guidance or "").strip():
            parts.append(
                "Guidance for persistent character memory:\n" + agent.memory_ltm_guidance.strip()
            )
        _append_world_wiki_parts(parts, agent)
        _append_foundry_sheet_and_roll_hint(parts, agent)
        _append_combat_context(parts, state, agent)
        ltm = db.list_long_term_for_agent(state.conn, agent.id)
        if ltm:
            buf = "Character memory (facts the PC would recall):\n"
            for _, text, _ in ltm[:24]:
                buf += f"- {text}\n"
            parts.append(buf)
        merged = "\n\n".join(parts)
        msgs: list[tuple[str, str]] = [("system", merged)]
        short = db.list_short_term(state.conn, agent.id, agent.memory_short_term_limit)
        for role, content in short:
            if role in ("user", "assistant"):
                msgs.append((role, content))
        t = topic.strip()
        if turn_index == 0:
            u = (
                f"(Out-of-combat banter with party members: {peer_names}. "
                + (f"Topic seed: {t}. " if t else "")
                + "You speak first — one or two short sentences in character. Dialogue only; no [[fas-...]] directives.)"
            )
        else:
            u = (
                f"(Continue light banter. {prev_speaker_name or 'Another PC'} spoke last. "
                f"You are {agent.name}. Reply with one or two short sentences in character only. "
                "No [[fas-...]] directives.)"
            )
        msgs.append(("user", u))
    return msgs


def format_chat_html(text: str) -> str:
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<p>{esc}</p>"


def _copy_db_if_missing(src: Path, dst: Path) -> None:
    if src.is_file() and not dst.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


async def _remember_semantic(
    state: AppState,
    agent: Agent,
    ollama_base: str,
    user_line: str,
    assistant_line: str,
) -> None:
    summary = f"Player said: {user_line}\nCharacter replied: {assistant_line}"
    try:
        emb_query = await embed_text(ollama_base, agent.embedding_model, summary)
    except Exception:
        return

    def _rows():
        with state.lock:
            return db.list_long_term_for_agent(state.conn, agent.id)

    rows = await asyncio.to_thread(_rows)
    best_sim = 0.0
    for _, _text, emb_blob in rows:
        if emb_blob:
            v = bytes_to_f32_vec(emb_blob)
            if len(v) == len(emb_query):
                s = cosine_similarity(emb_query, v)
                if s > best_sim:
                    best_sim = s
    if best_sim > 0.92:
        return
    emb_bytes = f32_vec_to_bytes(emb_query)

    def _ins():
        with state.lock:
            db.insert_long_term(state.conn, agent.id, "interaction", summary, emb_bytes)

    await asyncio.to_thread(_ins)


def _parse_ltm_facts_json(raw: str) -> list[str]:
    """Extract fact strings from model JSON output (allows stray markdown fences)."""
    text = raw.strip()
    if "```" in text:
        for block in text.split("```"):
            b = block.strip()
            if b.lower().startswith("json"):
                b = b[4:].strip()
            if b.startswith("{"):
                text = b
                break
    lo = text.find("{")
    hi = text.rfind("}")
    if lo < 0 or hi <= lo:
        return []
    try:
        data = json.loads(text[lo : hi + 1])
    except json.JSONDecodeError:
        return []
    facts = data.get("facts")
    if facts is None:
        facts = data.get("remember", [])
    if not isinstance(facts, list):
        return []
    out: list[str] = []
    for f in facts[:8]:
        s = str(f).strip()
        if len(s) >= 2 and len(s) <= 2000:
            out.append(s)
    return out


async def _remember_agent_curated(
    state: AppState,
    agent: Agent,
    ollama_base: str,
    user_line: str,
    assistant_line: str,
) -> None:
    """Second call: same character model outputs JSON facts to store as long-term memory."""
    sys = agent.full_system_prompt()
    task = (
        "\n\n---\nMemory task (not in-character dialogue): From the exchange below, decide what your "
        "character would keep as lasting memory. Reply with ONLY valid JSON, no markdown, exactly:\n"
        '{"facts": ["short standalone memory", ...]}\n'
        "Use {\"facts\": []} if nothing should be stored. At most 6 items; each line one discrete memory."
    )
    msgs = [
        ("system", sys + task),
        (
            "user",
            f"Exchange context:\n{user_line}\n\nYour last in-character reply:\n{assistant_line}",
        ),
    ]
    try:
        raw = await chat_completion(ollama_base, agent.model, 0.0, msgs)
    except Exception:
        return
    facts = _parse_ltm_facts_json(raw)
    if not facts:
        return

    for fact in facts:
        try:
            emb = await embed_text(ollama_base, agent.embedding_model, fact)
        except Exception:
            continue
        emb_bytes = f32_vec_to_bytes(emb)

        def ins():
            with state.lock:
                if db.long_term_content_exists(state.conn, agent.id, fact):
                    return
                db.insert_long_term(state.conn, agent.id, "fact", fact, emb_bytes)

        await asyncio.to_thread(ins)


async def _apply_ltm_after_exchange(
    state: AppState,
    agent: Agent,
    ollama_base: str,
    user_line: str,
    assistant_line: str,
) -> None:
    if not await should_run_ltm_semantic(ollama_base, agent, user_line, assistant_line):
        return
    if agent.memory_ltm_agent_curated:
        await _remember_agent_curated(state, agent, ollama_base, user_line, assistant_line)
    else:
        await _remember_semantic(state, agent, ollama_base, user_line, assistant_line)


# Request body models must live at module scope. Classes nested inside create_app() are not treated
# as JSON bodies by FastAPI (422: field required in query).


class NewAgentIn(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: Optional[str] = Field(None, alias="systemPrompt")
    model: Optional[str] = None
    temperature: Optional[float] = None
    memory_short_term_limit: Optional[int] = Field(None, alias="memoryShortTermLimit")
    memory_long_term_enabled: Optional[bool] = Field(None, alias="memoryLongTermEnabled")
    embedding_model: Optional[str] = Field(None, alias="embeddingModel")
    voice_provider: Optional[str] = Field(None, alias="voiceProvider")
    voice_model: Optional[str] = Field(None, alias="voiceModel")
    stt_provider: Optional[str] = Field(None, alias="sttProvider")
    voice_settings_json: Optional[str] = Field(None, alias="voiceSettingsJson")
    foundry_user_id: Optional[str] = Field(None, alias="foundryUserId")
    foundry_actor_id: Optional[str] = Field(None, alias="foundryActorId")
    foundry_world_id: Optional[str] = Field(None, alias="foundryWorldId")
    role: Optional[str] = None
    knowledge_scope: Optional[str] = Field(None, alias="knowledgeScope")
    is_enabled: Optional[bool] = Field(None, alias="isEnabled")
    memory_stm_guidance: Optional[str] = Field(None, alias="memoryStmGuidance")
    memory_ltm_guidance: Optional[str] = Field(None, alias="memoryLtmGuidance")
    memory_stm_filter: Optional[str] = Field(None, alias="memoryStmFilter")
    memory_ltm_filter: Optional[str] = Field(None, alias="memoryLtmFilter")
    memory_ltm_agent_curated: Optional[bool] = Field(None, alias="memoryLtmAgentCurated")
    world_wiki_url: Optional[str] = Field(None, alias="worldWikiUrl")
    world_wiki_notes: Optional[str] = Field(None, alias="worldWikiNotes")
    foundry_sheet_snapshot: Optional[str] = Field(None, alias="foundrySheetSnapshot")

    model_config = {"populate_by_name": True}


class UpdateAgentIn(BaseModel):
    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = Field(None, alias="systemPrompt")
    model: Optional[str] = None
    temperature: Optional[float] = None
    memory_short_term_limit: Optional[int] = Field(None, alias="memoryShortTermLimit")
    memory_long_term_enabled: Optional[bool] = Field(None, alias="memoryLongTermEnabled")
    embedding_model: Optional[str] = Field(None, alias="embeddingModel")
    voice_provider: Optional[str] = Field(None, alias="voiceProvider")
    voice_model: Optional[str] = Field(None, alias="voiceModel")
    stt_provider: Optional[str] = Field(None, alias="sttProvider")
    voice_settings_json: Optional[str] = Field(None, alias="voiceSettingsJson")
    foundry_user_id: Optional[str] = Field(None, alias="foundryUserId")
    foundry_actor_id: Optional[str] = Field(None, alias="foundryActorId")
    foundry_world_id: Optional[str] = Field(None, alias="foundryWorldId")
    role: Optional[str] = None
    knowledge_scope: Optional[str] = Field(None, alias="knowledgeScope")
    is_enabled: Optional[bool] = Field(None, alias="isEnabled")
    memory_stm_guidance: Optional[str] = Field(None, alias="memoryStmGuidance")
    memory_ltm_guidance: Optional[str] = Field(None, alias="memoryLtmGuidance")
    memory_stm_filter: Optional[str] = Field(None, alias="memoryStmFilter")
    memory_ltm_filter: Optional[str] = Field(None, alias="memoryLtmFilter")
    memory_ltm_agent_curated: Optional[bool] = Field(None, alias="memoryLtmAgentCurated")
    world_wiki_url: Optional[str] = Field(None, alias="worldWikiUrl")
    world_wiki_notes: Optional[str] = Field(None, alias="worldWikiNotes")
    foundry_sheet_snapshot: Optional[str] = Field(None, alias="foundrySheetSnapshot")

    model_config = {"populate_by_name": True}


class ConfigKV(BaseModel):
    key: str
    value: str


class ChatIn(BaseModel):
    agent_id: str = Field(alias="agentId")
    user_message: str = Field("", alias="userMessage")
    party_followup: bool = Field(False, alias="partyFollowup")
    model_config = {"populate_by_name": True}


class BanterIn(BaseModel):
    max_turns: int = Field(4, ge=2, le=12, alias="maxTurns")
    topic: str = ""
    model_config = {"populate_by_name": True}


class MemorySearchIn(BaseModel):
    agent_id: str = Field(alias="agentId")
    query: str
    top_k: int = Field(8, alias="topK")
    model_config = {"populate_by_name": True}


class MemoryEmbedIn(BaseModel):
    agent_id: str = Field(alias="agentId")
    text: str
    model_config = {"populate_by_name": True}


class PiperSynthIn(BaseModel):
    text: str
    model_file: str = Field(alias="modelFile")
    model_config = {"populate_by_name": True}


def create_app(static_dir: Optional[Path] = None) -> FastAPI:
    conn = db.open_db(db_path())
    st = AppState(conn=conn, lock=threading.Lock())

    app = FastAPI(title="Foundry Agent Studio", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.fas = st

    def _agent_json(a: Agent) -> dict[str, Any]:
        return db.agent_to_row(a)

    # ——— API: agents & config ———

    @app.get("/api/agents")
    async def agents_list() -> list[dict[str, Any]]:
        def _():
            with st.lock:
                return [_agent_json(x) for x in db.list_agents(st.conn)]

        return await asyncio.to_thread(_)

    @app.post("/api/agents")
    async def agents_create(new_agent: NewAgentIn) -> dict[str, Any]:
        def _():
            with st.lock:
                a = db.insert_agent(st.conn, new_agent.model_dump(by_alias=True, exclude_none=True))
                return _agent_json(a)

        return await asyncio.to_thread(_)

    @app.patch("/api/agents")
    async def agents_update(patch: UpdateAgentIn) -> dict[str, Any]:
        def _():
            with st.lock:
                try:
                    a = db.update_agent(st.conn, patch.model_dump(by_alias=True, exclude_unset=True))
                    return _agent_json(a)
                except KeyError:
                    raise HTTPException(404, "agent not found")

        try:
            return await asyncio.to_thread(_)
        except HTTPException:
            raise

    @app.delete("/api/agents/{agent_id}")
    async def agents_delete(agent_id: str) -> dict[str, str]:
        def _():
            with st.lock:
                try:
                    db.delete_agent(st.conn, agent_id)
                except KeyError:
                    raise HTTPException(404, "agent not found")

        try:
            await asyncio.to_thread(_)
        except HTTPException:
            raise
        return {"ok": "true"}

    @app.post("/api/agents/{agent_id}/wiki/refresh")
    async def agents_wiki_refresh(agent_id: str) -> dict[str, Any]:
        """Force-fetch the agent's world wiki URL and update cached text (server-side)."""

        def get() -> Agent:
            with st.lock:
                return db.get_agent(st.conn, agent_id)

        try:
            agent = await asyncio.to_thread(get)
        except KeyError:
            raise HTTPException(404, "agent not found")
        if not (agent.world_wiki_url or "").strip():
            raise HTTPException(400, "Set world wiki URL first")
        agent = await refresh_agent_wiki_cache_if_needed(st, agent, force=True)
        return _agent_json(agent)

    @app.get("/api/config/{key}")
    async def config_get(key: str) -> Optional[str]:
        def _():
            with st.lock:
                return db.get_config(st.conn, key)

        return await asyncio.to_thread(_)

    @app.post("/api/config")
    async def config_set(kv: ConfigKV) -> dict[str, bool]:
        def _():
            with st.lock:
                db.set_config(st.conn, kv.key, kv.value)

        await asyncio.to_thread(_)
        return {"ok": True}

    @app.get("/api/settings/storage")
    async def settings_storage_get() -> dict[str, Any]:
        ov = read_data_directory_override()
        return {
            "dataDirectory": str(ov) if ov is not None else "",
            "effectiveDataDirectory": str(effective_data_directory()),
            "dbPath": str(db_path()),
            "bootstrapPath": str(bootstrap_paths_file()),
        }

    @app.post("/api/settings/storage")
    async def settings_storage_post(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = (body.get("dataDirectory") or body.get("data_directory") or "").strip()
        old_db = db_path()
        if not raw:
            write_data_directory_override(None)
            default_db = default_app_data_dir() / "foundry_agent_studio.db"
            _copy_db_if_missing(old_db, default_db)
            return {
                "ok": True,
                "restartRequired": True,
                "message": "Using default data folder. Restart the app to open the database there.",
            }
        new_root = Path(raw).expanduser().resolve()
        new_root.mkdir(parents=True, exist_ok=True)
        new_db = new_root / "foundry_agent_studio.db"
        _copy_db_if_missing(old_db, new_db)
        write_data_directory_override(str(new_root))
        return {
            "ok": True,
            "restartRequired": True,
            "message": f"Data directory set to {new_root}. Restart the app to load settings from the new database path.",
        }

    @app.get("/api/bridge/status")
    async def bridge_status() -> dict[str, Any]:
        def _():
            with st.lock:
                port_s = db.get_config(st.conn, "bridge_port") or "17890"
                try:
                    port = int(port_s)
                except ValueError:
                    port = 17890
                secret = db.get_config(st.conn, "bridge_secret") or ""
                ob = db.get_config(st.conn, "ollama_base") or "http://127.0.0.1:11434"
                return {"port": port, "secret": secret, "ollamaBase": ob}

        return await asyncio.to_thread(_)

    @app.get("/api/ollama/models")
    async def ollama_list_models_ep() -> dict[str, Any]:
        """Installed names from Ollama plus curated suggestions for chat vs embedding."""
        base = _get_ollama_base(st)
        try:
            installed = await list_models(base)
        except Exception:
            installed = []
        return {
            "installed": installed,
            "suggestedChat": list(SUGGESTED_CHAT_MODELS),
            "suggestedEmbed": list(SUGGESTED_EMBEDDING_MODELS),
        }

    @app.get("/api/ollama/health")
    async def ollama_health_ep() -> bool:
        base = _get_ollama_base(st)
        return await health(base)

    @app.post("/api/ollama/pull")
    async def ollama_pull_ep(data: dict[str, Any] = Body(...)) -> dict[str, Any]:
        # Nested Pydantic models inside create_app() are not treated as JSON body params (FastAPI
        # expects them as query); use an explicit Body(dict) like party_broadcast_ep.
        n = str(data.get("name") or "").strip()
        if not n:
            raise HTTPException(400, "name is required (e.g. llama3.2)")
        base = _get_ollama_base(st)
        try:
            return await pull_model(base, n)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/party/broadcast")
    async def party_broadcast_ep(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty text")

        def _():
            with st.lock:
                return db.append_party_line_all_enabled(st.conn, text)

        n = await asyncio.to_thread(_)
        if n == 0:
            raise HTTPException(400, "no enabled agents")
        return {"ok": True, "agentsUpdated": n}

    @app.post("/api/party/banter")
    async def party_banter_ep(banter: BanterIn) -> dict[str, Any]:
        """Round-robin in-character lines between enabled PCs; disabled when combat snapshot is active."""

        def load_agents() -> tuple[list[Agent], bool]:
            with st.lock:
                blob = get_combat_blob_from_conn(st.conn)
                agents = [a for a in db.list_agents(st.conn) if a.is_enabled and a.role == "player"]
                return agents, is_active_combat_snapshot(blob)

        agents, combat_active = await asyncio.to_thread(load_agents)
        if combat_active:
            raise HTTPException(
                400,
                "Party banter is disabled while Foundry reports an active combat encounter. End combat or wait for sync.",
            )
        if len(agents) < 2:
            raise HTTPException(400, "need at least two enabled player agents")

        max_turns = banter.max_turns
        topic = (banter.topic or "").strip()
        peer_names = ", ".join(a.name for a in agents)
        ollama_base = _get_ollama_base(st)
        lines: list[dict[str, Any]] = []
        prev_name: Optional[str] = None

        for turn in range(max_turns):
            speaker = agents[turn % len(agents)]
            speaker = await refresh_agent_wiki_cache_if_needed(st, speaker)
            msgs = _build_messages_banter(st, speaker, peer_names, topic, turn, prev_name)
            try:
                reply = await chat_completion(ollama_base, speaker.model, speaker.temperature, msgs)
            except Exception as e:
                raise HTTPException(502, str(e))
            clean, _ = parse_fas_directives(reply)
            clean = (clean.strip() or "…")[:500]

            def persist_banter() -> None:
                with st.lock:
                    db.append_banter_line_all_agents(st.conn, speaker, clean, agents)

            await asyncio.to_thread(persist_banter)
            lines.append({"agentId": speaker.id, "name": speaker.name, "text": clean})
            prev_name = speaker.name

        return {"ok": True, "turns": len(lines), "lines": lines}

    @app.post("/api/ollama/chat")
    async def ollama_chat_ep(chat: ChatIn) -> dict[str, str]:
        base = _get_ollama_base(st)

        def get_agent_sync():
            with st.lock:
                return db.get_agent(st.conn, chat.agent_id)

        try:
            agent = await asyncio.to_thread(get_agent_sync)
        except KeyError:
            raise HTTPException(404, "agent not found")
        if not agent.is_enabled:
            raise HTTPException(400, "agent is disabled")

        agent = await refresh_agent_wiki_cache_if_needed(st, agent)

        if chat.party_followup:
            msgs = _build_messages_party_reply(st, agent)
        else:
            if not (chat.user_message or "").strip():
                raise HTTPException(400, "userMessage required unless partyFollowup")
            msgs = _build_messages(st, agent, chat.user_message.strip())
        reply = await chat_completion(base, agent.model, agent.temperature, msgs)

        if chat.party_followup:
            uline = "[Party follow-up]"
        else:
            uline = chat.user_message.strip()

        stm_ok = await should_persist_stm_exchange(base, agent, uline, reply)

        def persist_stm():
            with st.lock:
                if not stm_ok:
                    return
                if chat.party_followup:
                    db.append_short_term(
                        st.conn, agent.id, "assistant", reply, agent.memory_short_term_limit
                    )
                else:
                    db.append_short_term(
                        st.conn, agent.id, "user", chat.user_message.strip(), agent.memory_short_term_limit
                    )
                    db.append_short_term(
                        st.conn, agent.id, "assistant", reply, agent.memory_short_term_limit
                    )

        await asyncio.to_thread(persist_stm)

        await _apply_ltm_after_exchange(st, agent, base, uline, reply)

        return {"reply": reply}

    @app.post("/api/ollama/chat/stream")
    async def ollama_chat_stream_ep(chat: ChatIn) -> StreamingResponse:
        base = _get_ollama_base(st)

        def get_agent_sync():
            with st.lock:
                return db.get_agent(st.conn, chat.agent_id)

        try:
            agent = await asyncio.to_thread(get_agent_sync)
        except KeyError:
            raise HTTPException(404, "agent not found")
        if not agent.is_enabled:
            raise HTTPException(400, "agent is disabled")

        agent = await refresh_agent_wiki_cache_if_needed(st, agent)

        if chat.party_followup:
            msgs = _build_messages_party_reply(st, agent)
        else:
            if not (chat.user_message or "").strip():
                raise HTTPException(400, "userMessage required unless partyFollowup")
            msgs = _build_messages(st, agent, chat.user_message.strip())

        async def gen():
            parts: list[str] = []
            try:
                async for chunk in chat_completion_stream_lines(
                    base, agent.model, agent.temperature, msgs
                ):
                    parts.append(chunk)
                    yield json.dumps({"chunk": chunk}) + "\n"
            except Exception as e:
                yield json.dumps({"error": str(e)}) + "\n"
                return

            text = "".join(parts)

            if chat.party_followup:
                uline = "[Party follow-up]"
            else:
                uline = chat.user_message.strip()

            stm_ok = await should_persist_stm_exchange(base, agent, uline, text)

            def persist_stm():
                with st.lock:
                    if not stm_ok:
                        return
                    if chat.party_followup:
                        db.append_short_term(
                            st.conn, agent.id, "assistant", text, agent.memory_short_term_limit
                        )
                    else:
                        db.append_short_term(
                            st.conn, agent.id, "user", chat.user_message.strip(), agent.memory_short_term_limit
                        )
                        db.append_short_term(
                            st.conn, agent.id, "assistant", text, agent.memory_short_term_limit
                        )

            await asyncio.to_thread(persist_stm)

            await _apply_ltm_after_exchange(st, agent, base, uline, text)

            yield json.dumps({"done": True}) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    @app.post("/api/memory/embed")
    async def memory_embed_ep(req: MemoryEmbedIn) -> dict[str, bool]:
        base = _get_ollama_base(st)

        def get_agent_sync():
            with st.lock:
                return db.get_agent(st.conn, req.agent_id)

        try:
            agent = await asyncio.to_thread(get_agent_sync)
        except KeyError:
            raise HTTPException(404, "agent not found")

        v = await embed_text(base, agent.embedding_model, req.text)
        emb = f32_vec_to_bytes(v)

        def ins():
            with st.lock:
                db.insert_long_term(st.conn, req.agent_id, "fact", req.text, emb)

        await asyncio.to_thread(ins)
        return {"ok": True}

    @app.post("/api/memory/search")
    async def memory_search_ep(req: MemorySearchIn) -> list[str]:
        base = _get_ollama_base(st)

        def get_agent_sync():
            with st.lock:
                return db.get_agent(st.conn, req.agent_id)

        try:
            agent = await asyncio.to_thread(get_agent_sync)
        except KeyError:
            raise HTTPException(404, "agent not found")

        qv = await embed_text(base, agent.embedding_model, req.query)

        def rows():
            with st.lock:
                return db.list_long_term_for_agent(st.conn, req.agent_id)

        ltm = await asyncio.to_thread(rows)
        scored: list[tuple[float, str]] = []
        for _id, text, emb in ltm:
            if emb:
                v = bytes_to_f32_vec(emb)
                if len(v) == len(qv):
                    scored.append((cosine_similarity(qv, v), text))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in scored[: req.top_k]]

    @app.post("/api/ollama/launch")
    async def ollama_launch() -> dict[str, str]:
        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["ollama", "serve"],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
            else:
                subprocess.Popen(["ollama", "serve"], start_new_session=True)
            return {"message": "Started `ollama serve` in the background."}
        except Exception as e:
            raise HTTPException(
                500,
                f"Could not spawn Ollama ({e}). Install from https://ollama.com and ensure `ollama` is on PATH.",
            )

    # ——— Voice ———

    @app.get("/api/voice/mock/voices")
    async def voice_mock_voices() -> list[dict[str, str]]:
        return [
            {"id": "mock-1", "name": "Mock Voice", "provider": "mock"},
            {"id": "mock-2", "name": "Mock Alt", "provider": "mock"},
        ]

    @app.post("/api/voice/mock/synthesize")
    async def voice_mock_synth(body: dict[str, Any] = Body(...)) -> dict[str, str]:
        import base64

        text = body.get("text", "")
        raw = f"mock-wav:{text}".encode()
        return {"bytesBase64": base64.b64encode(raw).decode("ascii")}

    @app.post("/api/voice/mock/speak")
    async def voice_mock_speak(request: Request) -> dict[str, bool]:
        try:
            await request.json()
        except Exception:
            pass
        return {"ok": True}

    @app.post("/api/voice/mock/stt/transcribe")
    async def stt_mock(body: dict[str, Any] = Body(...)) -> dict[str, str]:
        import base64

        raw = body.get("audioBase64") or ""
        try:
            audio = base64.b64decode(raw)
        except Exception:
            audio = b""
        return {"text": f"[mock transcript {len(audio)} bytes]"}

    @app.get("/api/voice/local/paths")
    async def voice_local_paths() -> dict[str, Any]:
        def _():
            with st.lock:
                piper_path = db.get_config(st.conn, "piper_path") or ""
                piper_models_dir = db.get_config(st.conn, "piper_models_dir") or ""
                whisper_path = db.get_config(st.conn, "whisper_path") or ""
                whisper_model_path = db.get_config(st.conn, "whisper_model_path") or ""
            pr, pe = None, None
            wr, we = None, None
            try:
                p = resolve_sidecar_exe("piper", piper_path)
                pr = str(p)
            except FileNotFoundError as e:
                pe = str(e)
            if piper_tts_importable() and not pr:
                pr = "piper-tts (Python package)"
                pe = None
            try:
                w = resolve_whisper_cli_exe(whisper_path)
                wr = str(w)
            except FileNotFoundError as e:
                we = str(e)
            eff = effective_piper_models_dir_str(piper_models_dir)
            w_eff = effective_whisper_model_path_str(whisper_model_path)
            return {
                "piperPath": piper_path,
                "piperModelsDir": piper_models_dir,
                "piperModelsEffective": eff,
                "whisperPath": whisper_path,
                "whisperModelPath": whisper_model_path,
                "whisperModelEffective": w_eff,
                "piperExeResolved": pr,
                "piperExeError": pe,
                "piperTtsAvailable": piper_tts_importable(),
                "whisperExeResolved": wr,
                "whisperExeError": we,
            }

        return await asyncio.to_thread(_)

    @app.get("/api/voice/piper/catalog")
    async def voice_piper_catalog() -> list[dict[str, str]]:
        """Voice IDs from bundled list (`piper.download_voices`); files are <id>.onnx in models dir."""
        return [{"id": vid, "onnxFile": f"{vid}.onnx"} for vid in load_piper_voice_ids()]

    @app.get("/api/voice/piper/onnx")
    async def voice_piper_onnx() -> list[dict[str, str]]:
        def _():
            with st.lock:
                dir_s = db.get_config(st.conn, "piper_models_dir") or ""
            eff = effective_piper_models_dir_str(dir_s)
            if not eff:
                return []
            d = Path(eff)
            if not d.is_dir():
                return []
            out = []
            for p in sorted(d.glob("*.onnx")):
                out.append({"name": p.name, "path": str(p)})
            return out

        return await asyncio.to_thread(_)

    @app.post("/api/voice/piper/synthesize")
    async def voice_piper_synth(req: PiperSynthIn) -> dict[str, str]:
        def paths():
            with st.lock:
                user_piper = db.get_config(st.conn, "piper_path") or ""
                models_dir = db.get_config(st.conn, "piper_models_dir") or ""
            return user_piper, models_dir

        user_piper, models_dir = await asyncio.to_thread(paths)
        eff = effective_piper_models_dir_str(models_dir)
        if not eff:
            raise HTTPException(
                400,
                "Set piper_models_dir in Settings (folder with .onnx + .json), or keep it empty to use bundled voices.",
            )

        model = Path(eff) / req.model_file

        def run() -> bytes:
            if piper_tts_importable():
                try:
                    return synthesize_wav_piper_tts(model, req.text)
                except Exception as py_err:
                    try:
                        exe = resolve_sidecar_exe("piper", user_piper)
                        return run_piper_to_wav(exe, model, req.text)
                    except Exception:
                        raise py_err from None
            exe = resolve_sidecar_exe("piper", user_piper)
            return run_piper_to_wav(exe, model, req.text)

        try:
            wav = await asyncio.to_thread(run)
        except Exception as e:
            raise HTTPException(400, str(e))
        import base64

        return {"bytesBase64": base64.b64encode(wav).decode("ascii")}

    @app.post("/api/voice/whisper/transcribe")
    async def voice_whisper_tx(payload: dict[str, Any] = Body(...)) -> dict[str, str]:
        import base64

        raw = payload.get("audioBase64") or ""
        try:
            audio = base64.b64decode(raw)
        except Exception:
            raise HTTPException(400, "invalid audioBase64 (must be base64-encoded bytes)")
        if len(audio) < 200:
            raise HTTPException(
                400,
                "audio too small or empty after decode — hold the mic button longer or check the microphone permission.",
            )
        if not is_wav_header(audio):
            raise HTTPException(
                400,
                "Whisper expects 16-bit PCM WAV (RIFF/WAVE). If you did not use this app’s record button, "
                "convert to WAV or use mock STT in Settings.",
            )

        def paths():
            with st.lock:
                w = db.get_config(st.conn, "whisper_path") or ""
                m = db.get_config(st.conn, "whisper_model_path") or ""
            return w, m

        user_w, model_path_cfg = await asyncio.to_thread(paths)
        eff_model = effective_whisper_model_path_str(model_path_cfg)
        if not eff_model:
            raise HTTPException(
                400,
                "No Whisper model file: download ggml-small.bin into foundry_agent_studio/whisper_models/ "
                "(see README) or set Whisper model file in Local voice to a .ggml/.gguf path.",
            )

        def run():
            exe = resolve_whisper_cli_exe(user_w)
            model = Path(eff_model)
            wav = write_temp_wav(audio)
            txt_path = Path(str(wav) + ".txt")
            try:
                text = run_whisper_cli_to_text(exe, model, wav)
            finally:
                wav.unlink(missing_ok=True)
                txt_path.unlink(missing_ok=True)
            return text

        try:
            text = await asyncio.to_thread(run)
        except Exception as e:
            raise HTTPException(400, str(e))
        return {"text": text}

    # ——— Foundry bridge (same paths as Axum) ———

    def _verify_secret(x_fas_secret: Optional[str]) -> bool:
        with st.lock:
            sec = db.get_config(st.conn, "bridge_secret") or ""
        hdr = (x_fas_secret or "").strip()
        return bool(sec) and hdr == sec

    @app.get("/api/bridge/health")
    async def bridge_health() -> dict[str, Any]:
        return {"ok": True, "service": "foundry-agent-studio-bridge"}

    @app.get("/api/bridge/outbox")
    async def bridge_outbox() -> dict[str, Any]:
        with st.lock:
            items = st.outbox[:]
            st.outbox.clear()
        return {"items": items}

    @app.post("/api/bridge/event")
    async def bridge_event(
        request: Request,
        x_fas_secret: Optional[str] = Header(None, alias="X-FAS-Secret"),
    ) -> JSONResponse:
        if not _verify_secret(x_fas_secret):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        ev = await request.json()
        event_type = ev.get("type", "")
        payload = ev.get("payload") or {}

        if event_type == "world.connected":
            return JSONResponse({"ok": True})

        if event_type == "actor.sheet":
            actor_sheet_id = str(payload.get("actorId") or "")
            world_sheet_id = str(payload.get("worldId") or "")
            snapshot = payload.get("snapshot")
            if snapshot is None:
                return JSONResponse({"ok": True, "stored": False, "reason": "no snapshot"})

            def apply_sheet() -> bool:
                with st.lock:
                    ag = db.find_agent_by_foundry_actor(st.conn, actor_sheet_id, world_sheet_id)
                    if ag is None:
                        return False
                    try:
                        snap_json = json.dumps(snapshot, ensure_ascii=False)
                    except (TypeError, ValueError):
                        return False
                    if len(snap_json) > 50000:
                        snap_json = snap_json[:50000]
                    db.set_foundry_sheet_snapshot(st.conn, ag.id, snap_json)
                    return True

            stored = await asyncio.to_thread(apply_sheet)
            return JSONResponse({"ok": True, "stored": stored})

        if event_type == "combat.state":
            world_cs = str(payload.get("worldId") or "")
            combat_data = payload.get("combat")

            def save_combat() -> None:
                with st.lock:
                    blob = {
                        "worldId": world_cs,
                        "combat": combat_data,
                        "updatedAt": utc_now_rfc3339(),
                    }
                    raw = json.dumps(blob, ensure_ascii=False)
                    if len(raw) > 100000:
                        raw = raw[:100000]
                    db.set_config(st.conn, "foundry_combat_snapshot", raw)

            await asyncio.to_thread(save_combat)
            return JSONResponse({"ok": True})

        if event_type != "chat.received":
            return JSONResponse({"ok": True, "ignored": event_type})

        user_id = str(payload.get("userId") or "")
        actor_id = str(payload.get("actorId") or "")
        world_id = str(payload.get("worldId") or "")
        content = str(payload.get("content") or "")

        def find_agent():
            with st.lock:
                return db.find_responder_agent(st.conn, user_id, actor_id, world_id)

        agent = await asyncio.to_thread(find_agent)
        if agent is None:
            return JSONResponse({"ok": True, "handled": False})

        agent = await refresh_agent_wiki_cache_if_needed(st, agent)

        ollama_base = _get_ollama_base(st)
        msgs = _build_messages(st, agent, content)

        try:
            reply = await chat_completion(ollama_base, agent.model, agent.temperature, msgs)
        except Exception as e:
            return JSONResponse({"detail": str(e)}, status_code=502)

        clean_reply, fas_actions = parse_fas_directives(reply)
        with st.lock:
            combat_blob = get_combat_blob_from_conn(st.conn)
        fas_actions = filter_mechanical_actions(agent, fas_actions, combat_blob)
        stm_ok = await should_persist_stm_exchange(ollama_base, agent, content, clean_reply)

        def persist_stm():
            with st.lock:
                if not stm_ok:
                    return
                db.append_short_term(st.conn, agent.id, "user", content, agent.memory_short_term_limit)
                db.append_short_term(
                    st.conn, agent.id, "assistant", clean_reply, agent.memory_short_term_limit
                )

        await asyncio.to_thread(persist_stm)

        await _apply_ltm_after_exchange(st, agent, ollama_base, content, clean_reply)
        out: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "userId": agent.foundry_user_id,
            "actorId": agent.foundry_actor_id if agent.foundry_actor_id else None,
            "content": format_chat_html(clean_reply),
        }
        if fas_actions:
            out["actions"] = fas_actions
            out["rolls"] = [a for a in fas_actions if a.get("type") == "roll"]

        with st.lock:
            st.outbox.append(out)

        return JSONResponse({"ok": True, "handled": True, "agentId": agent.id})

    # ——— Static UI ———
    root = Path(__file__).resolve().parent.parent
    dist = static_dir or (root / "web" / "dist")
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="static")

    return app
