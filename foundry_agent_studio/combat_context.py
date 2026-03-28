"""Combat snapshot from Foundry (app_config) — turn awareness and directive gating."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from foundry_agent_studio.db import Agent


def _parse_blob(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw or not str(raw).strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def get_combat_blob_from_conn(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    from foundry_agent_studio import db

    return _parse_blob(db.get_config(conn, "foundry_combat_snapshot"))


def is_active_combat_snapshot(blob: Optional[dict[str, Any]]) -> bool:
    """True when Foundry sync shows an encounter with a turn order (banter should be disabled)."""
    if not blob:
        return False
    c = blob.get("combat")
    if not isinstance(c, dict):
        return False
    order = c.get("order")
    return isinstance(order, list) and len(order) > 0


def should_block_mechanical_actions(blob: Optional[dict[str, Any]], agent: Agent) -> bool:
    """
    True when tracked combat says it is not this agent's turn (mechanical directives disallowed).
    Unknown / not in combat order / no snapshot → do not block.
    """
    if not blob or not (agent.foundry_actor_id or "").strip():
        return False
    wid = blob.get("worldId") or ""
    if agent.foundry_world_id and wid and agent.foundry_world_id != wid:
        return False
    combat = blob.get("combat")
    if not isinstance(combat, dict):
        return False
    order = combat.get("order")
    if not isinstance(order, list) or not order:
        return False
    aid = agent.foundry_actor_id
    in_fight = any(
        isinstance(c, dict) and c.get("actorId") == aid for c in order
    )
    if not in_fight:
        return False
    try:
        turn_idx = int(combat.get("turnIndex", -1))
    except (TypeError, ValueError):
        return False
    if turn_idx < 0 or turn_idx >= len(order):
        return False
    cur = order[turn_idx]
    if not isinstance(cur, dict):
        return False
    return cur.get("actorId") != aid


def filter_mechanical_actions(
    agent: Agent,
    actions: list[dict[str, Any]],
    blob: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not actions or not should_block_mechanical_actions(blob, agent):
        return actions
    blocked = {"roll", "move_rel", "move_abs", "attack_item", "damage_item", "spell_item"}
    return [a for a in actions if a.get("type") not in blocked]


def format_combat_snapshot_for_prompt(
    conn: sqlite3.Connection,
    agent: Agent,
    max_chars: int = 8000,
) -> str:
    """Human-readable combat block for system context (per agent)."""
    if not (agent.foundry_actor_id or "").strip():
        return ""
    blob = get_combat_blob_from_conn(conn)
    if not blob:
        return ""

    wid = blob.get("worldId") or ""
    if agent.foundry_world_id and wid and agent.foundry_world_id != wid:
        return ""

    combat = blob.get("combat")
    updated = blob.get("updatedAt") or ""

    if combat is None:
        return (
            "Combat (Foundry sync): no active combat in the last snapshot from the VTT module. "
            "If you are in a fight, the GM may not be using the tracker or sync is delayed."
        )

    if not isinstance(combat, dict):
        return ""

    order = combat.get("order")
    if not isinstance(order, list):
        order = []

    scene_name = combat.get("sceneName") or ""
    round_n = combat.get("round", "?")
    turn_idx = combat.get("turnIndex")
    try:
        ti = int(turn_idx) if turn_idx is not None else -1
    except (TypeError, ValueError):
        ti = -1

    aid = agent.foundry_actor_id
    lines: list[str] = [
        "Combat snapshot (Foundry VTT — situational awareness only; GM rules):",
    ]
    if updated:
        lines.append(f"Snapshot time: {updated}.")
    if scene_name:
        lines.append(f"Scene: {scene_name}.")
    lines.append(f"Round {round_n}.")

    current_name = "?"
    is_my_turn = False
    if 0 <= ti < len(order) and isinstance(order[ti], dict):
        current = order[ti]
        current_name = str(current.get("name") or "?")
        is_my_turn = current.get("actorId") == aid

    in_order = any(
        isinstance(c, dict) and c.get("actorId") == aid for c in order
    )

    if not in_order:
        lines.append(
            "Your linked character does not appear in this encounter's turn order (spectator, "
            "not deployed, or different scene). Do not assume it is your turn unless chat makes it clear."
        )
    elif is_my_turn:
        lines.append(
            "**It is YOUR turn in the Foundry combat tracker.** Plan using **weapon/spell ranges** from your "
            "sheet snapshot. Target creatures with `|target:Name` or `|target:actorId` from the list below "
            "(e.g. `[[fas-attack:Longsword|target:Goblin]]`, `[[fas-spell:Fire Bolt|target:Goblin]]`, "
            "`[[fas-spell:Cure Wounds|target:Ally]]`). Use [[fas-damage:Item|crit]] after a confirmed hit where appropriate."
        )
    else:
        lines.append(
            f"**It is NOT your turn** (combat tracker: current actor ≈ {current_name}). "
            "Do not use [[fas-roll]], [[fas-move-rel]], [[fas-move-abs]], [[fas-attack]], [[fas-spell]], or "
            "[[fas-damage]]; speak in chat only if brief reaction is appropriate, or wait."
        )

    lines.append("Turn order (initiative order as synced):")
    for i, c in enumerate(order):
        if not isinstance(c, dict):
            continue
        name = c.get("name") or "?"
        init = c.get("initiative")
        marks: list[str] = []
        if c.get("actorId") == aid:
            marks.append("YOU")
        if i == ti:
            marks.append("current turn")
        if c.get("isDefeated"):
            marks.append("defeated/down")
        hp = c.get("hp")
        hp_s = ""
        if isinstance(hp, dict):
            v, m = hp.get("value"), hp.get("max")
            if v is not None:
                hp_s = f" HP {v}/{m if m is not None else '?'}"
        aid_c = c.get("actorId") or ""
        aid_note = f" actorId={aid_c}" if aid_c else ""
        line = f"  - {name}{hp_s} init {init}{aid_note}"
        if marks:
            line += f" [{', '.join(marks)}]"
        lines.append(line)

    lines.append(
        "Use these **names** or **actorId** values in `|target:...` on [[fas-attack]], [[fas-spell]], and "
        "[[fas-damage]]. Judge range and reach using weapon/spell **range** data in your sheet snapshot."
    )

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 24] + "\n… (truncated)"
    return out
