"""Combat snapshot from Foundry (app_config) — turn awareness and directive gating."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

from foundry_agent_studio.constants import (
    COMBAT_QUIET_SPEECH_RULES,
    COMBAT_TARGETING_INDEPENDENCE_HINT,
    DND5E_TURN_RESOURCES_HINT,
)
from foundry_agent_studio.db import Agent


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _weapon_name_suggests_ranged(name: str) -> bool:
    """When Foundry omits or flattens `range`, infer bows/crossbows/firearms from the item name."""
    n = (name or "").strip().lower()
    if not n:
        return False
    for frag in (
        "longbow",
        "shortbow",
        "crossbow",
        "light crossbow",
        "heavy crossbow",
        "hand crossbow",
        "sling",
        "blowgun",
        "musket",
        "pistol",
        "rifle",
        "javelin",
        "dart",
    ):
        if frag in n:
            return True
    # longbow / shortbow / etc.; avoids treating unrelated strings as bows
    return n.endswith("bow")


def _weapon_style_class(it: dict[str, Any]) -> str:
    """
    Rough 5e classification from Foundry item snapshot (range in feet).
    melee | reach | thrown | ranged | unknown
    """
    name = str(it.get("name") or "")
    rng = it.get("range")
    if not isinstance(rng, dict):
        return "ranged" if _weapon_name_suggests_ranged(name) else "unknown"
    v = _num(rng.get("value"))
    lg = _num(rng.get("long"))
    if v is None:
        return "ranged" if _weapon_name_suggests_ranged(name) else "unknown"
    if lg is not None and lg > v:
        if lg >= 100 or v >= 40:
            return "ranged"
        return "thrown"
    if v <= 5:
        return "melee"
    if v <= 10:
        return "reach"
    return "ranged"


def format_movement_budget_for_prompt(snapshot_json: str) -> str:
    """
    One line for the model: max grid squares per turn from synced walking speed (dnd5e snapshot).
    """
    raw = (snapshot_json or "").strip()
    if not raw:
        return ""
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""
    walk: int | None = None
    attr = snap.get("attributes")
    if isinstance(attr, dict):
        mov = attr.get("movement")
        if isinstance(mov, dict) and mov.get("walk") is not None:
            try:
                walk = int(float(mov["walk"]))
            except (TypeError, ValueError):
                pass
        if walk is None and attr.get("speed") is not None:
            try:
                walk = int(float(attr["speed"]))
            except (TypeError, ValueError):
                pass
    if walk is None:
        return (
            "**Movement budget:** Walking speed was not in the snapshot — assume **30 ft** (~**6** squares on a 5 ft grid) "
            "unless your sheet says otherwise. **Do not** move farther in one turn than that budget (before Dash/extra speed)."
        )
    squares = max(1, walk // 5)
    return (
        f"**Movement budget:** Walking speed **{walk} ft** this turn ≈ **{squares}** squares on a standard **5 ft** grid. "
        f"The **total** steps from all your `[[fas-move-rel]]` usage **this turn** must stay **≤ {squares}** "
        "(|dx|+|dy| per directive, summed); oversized moves are **clipped** in Foundry. "
        "Use **Dash** or the GM for more. "
        "`[[fas-move-abs]]` toward a distant cell also counts against this budget when the module can measure it."
    )


def format_engagement_style_from_sheet(agent: Agent) -> str:
    """
    Tell the model whether to fight at range or in melee based on **equipped** weapons in the sheet snapshot.
    """
    raw = (agent.foundry_sheet_snapshot or "").strip()
    if not raw:
        return ""
    try:
        snap = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(snap, dict):
        return ""
    items = snap.get("items")
    if not isinstance(items, list):
        return ""
    equipped_weapons: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("equipped"):
            continue
        t = (it.get("type") or "").lower()
        if t == "weapon":
            equipped_weapons.append(it)
        elif t == "equipment" and it.get("weaponType"):
            equipped_weapons.append(it)
    if not equipped_weapons:
        return (
            "**Engagement (from sheet):** No **equipped** weapon appears in the synced snapshot. "
            "**Same turn:** output `[[fas-equip:WeaponName|on]]` **then** `[[fas-attack:WeaponName|target:...]]` in **one** reply "
            "(equip **above** attack) — that is allowed and common; then follow that weapon’s reach."
        )
    rows: list[tuple[str, str, str]] = []
    styles: list[str] = []
    for w in equipped_weapons[:12]:
        name = str(w.get("name") or "?")
        st = _weapon_style_class(w)
        styles.append(st)
        rng = w.get("range")
        rs = ""
        if isinstance(rng, dict):
            vv, ll = rng.get("value"), rng.get("long")
            if ll is not None:
                rs = f"{vv}/{ll} ft"
            elif vv is not None:
                rs = f"{vv} ft"
        label = {
            "melee": "close combat (close distance)",
            "reach": "reach weapon (stay just outside 5-ft if helpful)",
            "thrown": "thrown / short ranged",
            "ranged": "distance fighter (keep at range)",
            "unknown": "check range on sheet",
        }.get(st, "check range on sheet")
        rows.append((name, rs, label))

    has_ranged = any(s in ("ranged", "thrown") for s in styles)
    has_melee = any(s in ("melee", "reach") for s in styles)

    out: list[str] = [
        "**Engagement style (equipped weapons — use tactics that match reach):**",
    ]
    for name, rs, label in rows:
        out.append(f"  - **{name}** ({rs or 'range ?'}): {label}")
    out.append("")
    if has_ranged and not has_melee:
        out.append(
            "You are set up as a **distance fighter** with these equipped weapons: **stay at range**, use **cover** and "
            "**[[fas-move-rel]]** to avoid being surrounded, and **each turn** attack with "
            "`[[fas-attack:ExactWeaponName|target:...]]` while foes are within the weapon’s long/short range — "
            "include that line **in the same reply** as any movement."
        )
    elif has_melee and not has_ranged:
        out.append(
            "You are set up as a **close-up fighter**: **close with enemies** to get within reach, then "
            "`[[fas-attack:ExactWeaponName|target:...]]`. Use movement to **engage** priority targets when safe."
        )
    elif has_ranged and has_melee:
        out.append(
            "You have **both** melee- and ranged-style weapons equipped: **default to ranged** (longbow/crossbow/etc.) "
            "whenever a hostile is **not** adjacent — **do not** spend your whole turn closing if you could already shoot. "
            "Use **melee** `[[fas-attack:ExactMeleeName|target:...]]` when already next to the target or when you must finish "
            "someone in reach. **Same turn:** after any `[[fas-move-rel]]`, include `[[fas-attack:...]]` — movement alone does "
            "not use your Action."
        )
    else:
        out.append(
            "Use **sheet snapshot** weapon ranges (feet) and **battlefield** distances. If you have a **bow or crossbow**, "
            "**attack at range** rather than only repositioning — grid steps are not the same as feet, but if foes are "
            "clearly not adjacent, **shoot** with `[[fas-attack:...|target:...]]`."
        )
    out.append(
        "**Each combat reply:** optional **`[[fas-equip]]`**, **one** move, **one** `[[fas-attack|target:...]]` or offensive `[[fas-spell]]` — "
        "then stop; damage on a hit is automatic in Foundry."
    )
    return "\n".join(out)


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


def _distance_rows_for_actor(dist_map: Any, my_aid: str) -> Optional[list[Any]]:
    """
    distancesFromActors keys must match this agent's Foundry actor id. JSON/clients sometimes differ
    slightly in string form — try exact match then any key equal after strip.
    """
    if not isinstance(dist_map, dict) or not (my_aid or "").strip():
        return None
    aid = my_aid.strip()
    rows = dist_map.get(aid)
    if isinstance(rows, list):
        return rows
    for k, v in dist_map.items():
        if str(k).strip() == aid and isinstance(v, list):
            return v
    return None


def _nearest_hostile_target_from_combat_order(
    agent: Agent,
    combat_blob: Optional[dict[str, Any]],
) -> Optional[str]:
    """When map distances are missing or keys do not match, use combat order + token disposition from sync."""
    if not combat_blob:
        return None
    wid = combat_blob.get("worldId") or ""
    if agent.foundry_world_id and wid and agent.foundry_world_id != wid:
        return None
    combat = combat_blob.get("combat")
    if not isinstance(combat, dict):
        return None
    order = combat.get("order")
    if not isinstance(order, list):
        return None
    my_aid = (agent.foundry_actor_id or "").strip()
    for c in order:
        if not isinstance(c, dict):
            continue
        oid = (c.get("actorId") or "").strip()
        if oid == my_aid:
            continue
        if c.get("isDefeated"):
            continue
        disp = (c.get("disposition") or "").strip().lower()
        if disp != "hostile":
            continue
        if oid:
            return oid
        nm = (c.get("name") or "").strip()
        if nm:
            return nm
    return None


def is_active_combat_snapshot(blob: Optional[dict[str, Any]]) -> bool:
    """True when Foundry sync shows an encounter with a turn order (banter should be disabled)."""
    if not blob:
        return False
    c = blob.get("combat")
    if not isinstance(c, dict):
        return False
    order = c.get("order")
    return isinstance(order, list) and len(order) > 0


def _nearest_hostile_target_query_from_battlefield(
    agent: Agent,
    combat_blob: Optional[dict[str, Any]],
) -> Optional[str]:
    """Prefer actorId, else display name, for closest non-defeated hostile in distancesFromActors."""
    if not combat_blob:
        return None
    wid = combat_blob.get("worldId") or ""
    if agent.foundry_world_id and wid and agent.foundry_world_id != wid:
        return None
    bf = combat_blob.get("battlefield")
    if not isinstance(bf, dict):
        return None
    dist_map = bf.get("distancesFromActors")
    my_aid = (agent.foundry_actor_id or "").strip()
    if not isinstance(dist_map, dict) or not my_aid:
        return None
    rows = _distance_rows_for_actor(dist_map, my_aid)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("defeated"):
            continue
        disp = (row.get("disposition") or "").strip().lower()
        if disp != "hostile":
            continue
        oid = (row.get("actorId") or "").strip()
        if oid:
            return oid
        nm = (row.get("name") or "").strip()
        if nm:
            return nm
    return None


def ensure_explicit_targets_on_attack_spell_actions(
    agent: Agent,
    actions: list[dict[str, Any]],
    combat_blob: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Attacks/spells should always carry targetQuery for Foundry. If the model omitted |target:...|,
    fill with the closest hostile from battlefield sync (actorId or name), else the literal `nearest`.
    """
    fallback = (
        _nearest_hostile_target_query_from_battlefield(agent, combat_blob)
        or _nearest_hostile_target_from_combat_order(agent, combat_blob)
        or "nearest"
    )
    out: list[dict[str, Any]] = []
    for a in actions:
        t = a.get("type")
        if t not in ("attack_item", "spell_item"):
            out.append(dict(a))
            continue
        q = (a.get("targetQuery") or "").strip()
        if q:
            out.append(dict(a))
            continue
        b = dict(a)
        b["targetQuery"] = fallback
        logger.info(
            "FAS injected targetQuery (model omitted |target|): action_type=%s targetQuery=%s",
            t,
            fallback,
        )
        out.append(b)
    return out


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
    blocked = {"roll", "move_rel", "move_abs", "attack_item", "damage_item", "spell_item", "equip_item"}
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

    if in_order:
        eg = format_engagement_style_from_sheet(agent)
        if eg:
            lines.append(eg)
            lines.append("")

    if not in_order:
        lines.append(
            "Your linked character does not appear in this encounter's turn order (spectator, "
            "not deployed, or different scene). Do not assume it is your turn unless chat makes it clear."
        )
    elif is_my_turn:
        lines.append(
            "**No `//` pseudo-actions or plain-English plans** — only `[[fas-move-rel]]`, `[[fas-equip]]`, `[[fas-attack]]`, etc. "
            "Chat that is not `[[fas-...]]` does **nothing** in Foundry."
        )
        lines.append(DND5E_TURN_RESOURCES_HINT.strip())
        lines.append(COMBAT_TARGETING_INDEPENDENCE_HINT.strip())
        lines.append(
            "**It is YOUR turn in the Foundry combat tracker.** **Do not write in-character chat** on this turn — "
            "output **only** [[fas-...]] directives (moves, attacks, spells) as needed. "
            "**One packet:** optional equip, **one** move, **one** `[[fas-attack:...|target:Hostile]]` or offensive `[[fas-spell]]` (server drops extras). "
            "**You must try to attack or cast an offensive spell** if any enemy "
            "is in range of a weapon or spell you have **after** movement. **Ranged (e.g. Longbow):** a few squares away is often **in range**. "
            "Do **not** output only moves when you could still `[[fas-attack]]` or `[[fas-spell]]`. "
            "Use **weapon/spell ranges** from your sheet snapshot; target with `|target:Name` or `|target:actorId` "
            "from the list below (e.g. `[[fas-attack:<your weapon name>|target:Goblin]]`, `[[fas-spell:Fire Bolt|target:Goblin]]`, "
            "`[[fas-spell:Cure Wounds|target:Ally]]`). **Weapon** attacks: `[[fas-attack:...]]` only — the module rolls **damage** "
            "and applies **critical** damage when the attack roll is a critical; do **not** add `[[fas-damage]]`."
        )
    else:
        lines.append(
            f"**It is NOT your turn** (combat tracker: current actor ≈ {current_name}). "
            "Do not use [[fas-roll]], [[fas-move-rel]], [[fas-move-abs]], [[fas-attack]], [[fas-spell]], or "
            "[[fas-damage]]. **Do not chat** except a **≤~6 second** tactical line to **that** character if you must "
            "instruct them on **their** turn; otherwise stay silent."
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
        disp = c.get("disposition") or "unknown"
        line = f"  - {name} ({disp}){hp_s} init {init}{aid_note}"
        if marks:
            line += f" [{', '.join(marks)}]"
        lines.append(line)

    if isinstance(order, list) and len(order) > 0:
        lines.append("")
        lines.append(COMBAT_QUIET_SPEECH_RULES.strip())

    lines.append(
        "Use these **names** or **actorId** values in `|target:...` on [[fas-attack]], [[fas-spell]], and "
        "[[fas-damage]]. Judge range and reach using weapon/spell **range** data in your sheet snapshot."
    )

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 24] + "\n… (truncated)"
    return out


def format_battlefield_snapshot_for_prompt(
    conn: sqlite3.Connection,
    agent: Agent,
    max_chars: int = 12000,
    max_wall_lines: int = 120,
) -> str:
    """Grid positions, combatant tokens, and wall segments from Foundry canvas (when module sends battlefield)."""
    if not (agent.foundry_actor_id or "").strip():
        return ""
    blob = get_combat_blob_from_conn(conn)
    if not blob:
        return ""

    wid = blob.get("worldId") or ""
    if agent.foundry_world_id and wid and agent.foundry_world_id != wid:
        return ""

    bf = blob.get("battlefield")
    if not isinstance(bf, dict):
        return ""

    lines: list[str] = [
        "Battlefield layout (from Foundry scene; use with combat snapshot above):",
    ]
    vn = bf.get("visibilityNote")
    if isinstance(vn, str) and vn.strip():
        lines.append(vn.strip())
    cn = bf.get("coordinateNote")
    if isinstance(cn, str) and cn.strip():
        lines.append(cn.strip())

    sn = bf.get("sceneName") or ""
    cols = bf.get("sceneGridSize") if isinstance(bf.get("sceneGridSize"), dict) else {}
    grid_w = cols.get("columns")
    grid_h = cols.get("rows")
    gsp = bf.get("gridSizePixels")
    gridless = bool(bf.get("gridless"))
    lines.append(
        f"Scene: {sn or '(unknown)'} | "
        f"{'gridless (pixels)' if gridless else f'grid ~{grid_w}×{grid_h} cells'}"
        f"{f' | ~{gsp}px per cell' if gsp else ''}"
    )

    tok_list = bf.get("tokens")
    dist_map = bf.get("distancesFromActors")
    my_aid = (agent.foundry_actor_id or "").strip()
    my_rows = _distance_rows_for_actor(dist_map, my_aid) if isinstance(dist_map, dict) else None
    if my_rows:
        lines.append(
            "Distances **from your token** to other combatants on the map (grid units; lower = closer). "
            "**Ranged** weapons use **feet** on the item (short/long range) — you do **not** need to be adjacent unless using melee. "
            "Use this to **choose targets** and judge reach/range — you are not required to pick the nearest enemy."
        )
        for row in my_rows[:28]:
            if not isinstance(row, dict):
                continue
            nm = row.get("name") or "?"
            disp = row.get("disposition") or "?"
            d = row.get("gridDistance")
            oid = row.get("actorId") or ""
            hp = row.get("hp")
            defc = row.get("defeated")
            hp_s = ""
            if isinstance(hp, dict) and hp.get("value") is not None:
                hp_s = f" HP {hp.get('value')}/{hp.get('max', '?')}"
            mark = " (defeated)" if defc else ""
            lines.append(f"  → {nm} ({disp}) distance≈{d}{hp_s} actorId={oid}{mark}")

    if isinstance(tok_list, list) and tok_list:
        lines.append("Combatant tokens (disposition is token disposition on canvas):")
        for t in tok_list:
            if not isinstance(t, dict):
                continue
            nm = t.get("name") or "?"
            disp = t.get("disposition") or "?"
            aid = t.get("actorId") or ""
            pos = t.get("position") if isinstance(t.get("position"), dict) else {}
            w = t.get("width")
            h = t.get("height")
            el = t.get("elevation")
            if gridless or pos.get("gridless"):
                px, py = pos.get("pixelX"), pos.get("pixelY")
                pos_s = f"pixel ~({px},{py})"
            elif "col" in pos and "row" in pos:
                pos_s = f"col {pos['col']}, row {pos['row']} (for [[fas-move-abs:col,row]])"
            else:
                pos_s = f"approx grid {pos.get('approxCol')}, {pos.get('approxRow')}"
            lines.append(
                f"  - {nm} ({disp}) {pos_s} actorId={aid} size {w}×{h} elev {el}"
            )
    else:
        lines.append("No token positions synced (canvas not ready or no tokens for combatants).")

    miss = bf.get("missingFromMap")
    if isinstance(miss, list) and miss:
        lines.append("Combatants without a visible map position (hidden token or not on scene):")
        for m in miss[:24]:
            if not isinstance(m, dict):
                continue
            lines.append(
                f"  - {m.get('name') or '?'} actorId={m.get('actorId') or ''} ({m.get('reason') or 'unknown'})"
            )

    if bf.get("wallsOmitted"):
        lines.append("Wall list omitted (payload size); geometry may be incomplete.")
    walls = bf.get("walls")
    if isinstance(walls, list) and walls:
        lines.append(
            f"Wall segments (up to {max_wall_lines}; scene total wall objects ≈ {bf.get('wallObjectsOnScene', '?')}) "
            "— grid endpoints when present, else scene pixels:"
        )
        for i, seg in enumerate(walls):
            if i >= max_wall_lines:
                lines.append(f"  … and {len(walls) - max_wall_lines} more segments")
                break
            if not isinstance(seg, dict):
                continue
            g = seg.get("grid") if isinstance(seg.get("grid"), dict) else None
            if g and all(k in g for k in ("colA", "rowA", "colB", "rowB")):
                seg_s = (
                    f"grid ({g['colA']},{g['rowA']})—({g['colB']},{g['rowB']})"
                )
            else:
                seg_s = f"px ({seg.get('ax')},{seg.get('ay')})—({seg.get('bx')},{seg.get('by')})"
            door = seg.get("door")
            extra = " [door]" if door else ""
            lines.append(f"  - {seg_s}{extra}")
        if bf.get("wallsTruncated"):
            lines.append("  (wall list truncated on the wire — simplify the scene or raise limits in code)")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 24] + "\n… (truncated)"
    return out
