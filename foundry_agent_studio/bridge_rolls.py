"""Parse Foundry VTT directives in agent replies: rolls, token moves (ordered)."""

from __future__ import annotations

import logging
import re
from typing import Any

from foundry_agent_studio.combat_context import _weapon_style_class

logger = logging.getLogger(__name__)

# [[fas-roll:1d20+5]] or [[fas-roll:2d6+3|fire damage]]
FAS_ROLL_PATTERN = re.compile(
    r"\[\[fas-roll:([^]|]+)(?:\|([^\]]*))?\]\]",
    re.IGNORECASE,
)

# [[fas-move-rel:2,-1]] — grid steps (square / hex: uses scene grid size on the client)
FAS_MOVE_REL_PATTERN = re.compile(
    r"\[\[fas-move-rel:\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\]",
    re.IGNORECASE,
)

# [[fas-move-abs:10,8]] — 0-based column gx, row gy (mapped to Foundry offset { i: gy, j: gx })
FAS_MOVE_ABS_PATTERN = re.compile(
    r"\[\[fas-move-abs:\s*(\d+)\s*,\s*(\d+)\s*\]\]",
    re.IGNORECASE,
)

# Inner body parsed for |target:Name and |crit — see _parse_*_pipe_options
FAS_ATTACK_PATTERN = re.compile(r"\[\[fas-attack:\s*([^\]]+)\]\]", re.IGNORECASE)

FAS_SPELL_PATTERN = re.compile(r"\[\[fas-spell:\s*([^\]]+)\]\]", re.IGNORECASE)

FAS_DAMAGE_PATTERN = re.compile(r"\[\[fas-damage:\s*([^\]]+)\]\]", re.IGNORECASE)

FAS_EQUIP_PATTERN = re.compile(r"\[\[fas-equip:\s*([^\]]+)\]\]", re.IGNORECASE)


def _parse_item_target_inner(inner: str) -> tuple[str, str | None]:
    """Item name plus optional |target:NameOrActorId from pipe segments."""
    inner = inner.strip()
    if not inner:
        return "", None
    parts = [p.strip() for p in inner.split("|")]
    item_name = parts[0]
    target_query: str | None = None
    for p in parts[1:]:
        pl = p.lower()
        if pl.startswith("target:"):
            target_query = p.split(":", 1)[1].strip()
    return item_name, target_query


def _parse_equip_inner(inner: str) -> tuple[str, bool | None]:
    """Item name and optional on/off; None means toggle."""
    inner = inner.strip()
    if not inner:
        return "", None
    parts = [p.strip() for p in inner.split("|")]
    item_name = parts[0]
    if len(parts) < 2:
        return item_name, None
    p2 = parts[1].lower()
    if p2 in ("on", "true", "1", "equip", "yes"):
        return item_name, True
    if p2 in ("off", "false", "0", "unequip", "no"):
        return item_name, False
    return item_name, None


def _parse_damage_inner(inner: str) -> tuple[str, bool, str | None]:
    """Item name, crit flag, optional |target: for healing/damage context."""
    inner = inner.strip()
    if not inner:
        return "", False, None
    parts = [p.strip() for p in inner.split("|")]
    item_name = parts[0]
    critical = False
    target_query: str | None = None
    for p in parts[1:]:
        pl = p.lower()
        if pl in ("crit", "critical"):
            critical = True
        elif pl.startswith("target:"):
            target_query = p.split(":", 1)[1].strip()
    return item_name, critical, target_query


def parse_fas_directives(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip all supported directives; return actions in **left-to-right** order for the module."""
    spans: list[tuple[int, int, dict[str, Any]]] = []

    for m in FAS_ROLL_PATTERN.finditer(text):
        formula = (m.group(1) or "").strip()
        flavor = (m.group(2) or "").strip()
        if formula:
            spans.append(
                (m.start(), m.end(), {"type": "roll", "formula": formula, "flavor": flavor})
            )

    for m in FAS_MOVE_REL_PATTERN.finditer(text):
        dx = int(m.group(1))
        dy = int(m.group(2))
        spans.append((m.start(), m.end(), {"type": "move_rel", "dx": dx, "dy": dy}))

    for m in FAS_MOVE_ABS_PATTERN.finditer(text):
        gx = int(m.group(1))
        gy = int(m.group(2))
        spans.append((m.start(), m.end(), {"type": "move_abs", "gx": gx, "gy": gy}))

    for m in FAS_ATTACK_PATTERN.finditer(text):
        item_name, target_q = _parse_item_target_inner(m.group(1) or "")
        if item_name:
            act: dict[str, Any] = {"type": "attack_item", "itemName": item_name}
            if target_q:
                act["targetQuery"] = target_q
            spans.append((m.start(), m.end(), act))

    for m in FAS_SPELL_PATTERN.finditer(text):
        item_name, target_q = _parse_item_target_inner(m.group(1) or "")
        if item_name:
            act = {"type": "spell_item", "itemName": item_name}
            if target_q:
                act["targetQuery"] = target_q
            spans.append((m.start(), m.end(), act))

    for m in FAS_DAMAGE_PATTERN.finditer(text):
        item_name, critical, target_q = _parse_damage_inner(m.group(1) or "")
        if item_name:
            dmg: dict[str, Any] = {
                "type": "damage_item",
                "itemName": item_name,
                "critical": critical,
            }
            if target_q:
                dmg["targetQuery"] = target_q
            spans.append((m.start(), m.end(), dmg))

    for m in FAS_EQUIP_PATTERN.finditer(text):
        item_name, eq = _parse_equip_inner(m.group(1) or "")
        if item_name:
            eq_act: dict[str, Any] = {"type": "equip_item", "itemName": item_name}
            if eq is not None:
                eq_act["equipped"] = eq
            spans.append((m.start(), m.end(), eq_act))

    spans.sort(key=lambda x: x[0])
    merged: list[tuple[int, int, dict[str, Any]]] = []
    prev_end = -1
    for start, end, action in spans:
        if start < prev_end:
            continue
        merged.append((start, end, action))
        prev_end = end

    out = text
    for start, end, _ in sorted(merged, key=lambda x: -x[0]):
        out = out[:start] + out[end:]

    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    actions = dedupe_redundant_weapon_damage_after_attack([a for _, _, a in merged])
    return out, actions


def dedupe_redundant_weapon_damage_after_attack(
    actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    The Foundry module auto-rolls weapon damage immediately after [[fas-attack]].
    Drop a following [[fas-damage]] for the same item when it is not a crit line, to avoid double damage.
    """
    if len(actions) < 2:
        return actions
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(actions):
        a = actions[i]
        if (
            a.get("type") == "attack_item"
            and i + 1 < len(actions)
            and actions[i + 1].get("type") == "damage_item"
        ):
            nxt = actions[i + 1]
            a_name = (a.get("itemName") or "").strip().lower()
            d_name = (nxt.get("itemName") or "").strip().lower()
            if a_name and a_name == d_name and not nxt.get("critical"):
                out.append(a)
                i += 2
                continue
        out.append(a)
        i += 1
    return out


def _item_dict_exact_name(item_dicts: list[dict[str, Any]], resolved: str) -> dict[str, Any] | None:
    """Sheet item dict whose name matches `resolved` (case-insensitive)."""
    rl = (resolved or "").strip().lower()
    if not rl:
        return None
    for it in item_dicts:
        if not isinstance(it, dict):
            continue
        n = (it.get("name") or "").strip()
        if n.lower() == rl:
            return it
    return None


def resolve_item_name_like_foundry(query: str, item_dicts: list[dict[str, Any]]) -> str | None:
    """
    Match item name like Foundry bridge.js findItemByName: exact, then substring (name includes query), then compact.
    """
    q = (query or "").strip().lower()
    if not q:
        return None
    pairs: list[tuple[str, str]] = []
    for it in item_dicts:
        if not isinstance(it, dict):
            continue
        n = (it.get("name") or "").strip()
        if n:
            pairs.append((n, n.lower()))
    for name, nl in pairs:
        if nl == q:
            return name
    for name, nl in pairs:
        if q in nl:
            return name
    cq = q.replace(" ", "")
    for name, nl in pairs:
        if nl.replace(" ", "") == cq:
            return name
    return None


def _fallback_weapon_name(item_dicts: list[dict[str, Any]]) -> str | None:
    """
    Prefer an **equipped ranged** weapon when remapping a hallucinated name so combat at distance
    still uses Longbow instead of the first equipped melee.
    """
    equipped_r: list[str] = []
    equipped_m: list[str] = []
    rest_r: list[str] = []
    rest_m: list[str] = []
    for it in item_dicts:
        if not isinstance(it, dict):
            continue
        t = (it.get("type") or "").lower()
        if not (t == "weapon" or (t == "equipment" and it.get("weaponType"))):
            continue
        n = (it.get("name") or "").strip()
        if not n:
            continue
        st = _weapon_style_class(it)
        rangedish = st in ("ranged", "thrown")
        if it.get("equipped"):
            (equipped_r if rangedish else equipped_m).append(n)
        else:
            (rest_r if rangedish else rest_m).append(n)
    if equipped_r:
        return equipped_r[0]
    if equipped_m:
        return equipped_m[0]
    if rest_r:
        return rest_r[0]
    if rest_m:
        return rest_m[0]
    return None


def _fallback_spell_name(item_dicts: list[dict[str, Any]]) -> str | None:
    for it in item_dicts:
        if not isinstance(it, dict):
            continue
        if (it.get("type") or "").lower() != "spell":
            continue
        n = (it.get("name") or "").strip()
        if n:
            return n
    return None


def sanitize_fas_actions_against_sheet(
    actions: list[dict[str, Any]],
    snapshot_json: str,
) -> list[dict[str, Any]]:
    """
    Drop impossible [[fas-equip:...]] (item not on actor) and remap unknown weapon names on
    [[fas-attack]] / [[fas-damage]] to a real weapon from the sheet so hallucinated names
    (e.g. Scimitar) still roll with Longbow/Dagger.
    """
    raw = (snapshot_json or "").strip()
    if not raw or not actions:
        return actions
    try:
        import json

        obj = json.loads(raw)
    except json.JSONDecodeError:
        return actions
    if not isinstance(obj, dict):
        return actions
    items = obj.get("items")
    if not isinstance(items, list):
        return actions
    item_dicts = [it for it in items if isinstance(it, dict)]
    if not item_dicts:
        return actions

    fb_weapon = _fallback_weapon_name(item_dicts)
    fb_spell = _fallback_spell_name(item_dicts)
    out: list[dict[str, Any]] = []

    for a in actions:
        t = a.get("type")
        if t == "equip_item":
            q = (a.get("itemName") or "").strip()
            resolved = resolve_item_name_like_foundry(q, item_dicts)
            if not resolved:
                logger.warning(
                    "Dropping fas-equip: item not on character sheet (model hallucination?): %r",
                    q,
                )
                continue
            itd = _item_dict_exact_name(item_dicts, resolved)
            want = a.get("equipped")
            cur = bool(itd.get("equipped")) if itd else False
            if want is True and cur:
                logger.info(
                    "Dropping redundant fas-equip|on (already equipped): %r",
                    resolved,
                )
                continue
            if want is False and not cur:
                logger.info(
                    "Dropping redundant fas-equip|off (already unequipped): %r",
                    resolved,
                )
                continue
            if want is None and cur:
                logger.info(
                    "Dropping bare fas-equip (already equipped; avoids toggle off): %r",
                    resolved,
                )
                continue
            b = dict(a)
            b["itemName"] = resolved
            out.append(b)
            continue
        if t in ("attack_item", "damage_item"):
            q = (a.get("itemName") or "").strip()
            resolved = resolve_item_name_like_foundry(q, item_dicts)
            if resolved:
                b = dict(a)
                b["itemName"] = resolved
                out.append(b)
            elif fb_weapon:
                logger.warning(
                    "Remapping unknown weapon in fas-attack/fas-damage %r -> %r",
                    q,
                    fb_weapon,
                )
                b = dict(a)
                b["itemName"] = fb_weapon
                out.append(b)
            else:
                logger.warning(
                    "Dropping fas-attack/fas-damage: no weapon on sheet, query was %r",
                    q,
                )
            continue
        if t == "spell_item":
            q = (a.get("itemName") or "").strip()
            resolved = resolve_item_name_like_foundry(q, item_dicts)
            if resolved:
                b = dict(a)
                b["itemName"] = resolved
                out.append(b)
            elif fb_spell:
                logger.warning(
                    "Remapping unknown spell %r -> %r",
                    q,
                    fb_spell,
                )
                b = dict(a)
                b["itemName"] = fb_spell
                out.append(b)
            else:
                logger.warning("Dropping fas-spell: spell not on sheet: %r", q)
            continue
        out.append(dict(a))

    return out


def _walk_speed_ft_from_snapshot(snapshot_json: str) -> float | None:
    raw = (snapshot_json or "").strip()
    if not raw:
        return None
    try:
        import json

        snap = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(snap, dict):
        return None
    attr = snap.get("attributes")
    if not isinstance(attr, dict):
        return None
    mov = attr.get("movement")
    if isinstance(mov, dict) and mov.get("walk") is not None:
        try:
            return float(mov["walk"])
        except (TypeError, ValueError):
            pass
    if attr.get("speed") is not None:
        try:
            return float(attr["speed"])
        except (TypeError, ValueError):
            pass
    return None


def _clamp_grid_delta_int(dx: int, dy: int, max_steps: int) -> tuple[int, int, int]:
    ax, ay = abs(dx), abs(dy)
    m = ax + ay
    if max_steps <= 0:
        return 0, 0, 0
    if m <= max_steps:
        return dx, dy, m
    sx = 1 if dx >= 0 else -1
    sy = 1 if dy >= 0 else -1
    rx = (ax * max_steps) // m
    ry = (ay * max_steps) // m
    used = rx + ry
    rem = max_steps - used
    while rem > 0:
        if ax >= ay and rx < ax:
            rx += 1
            rem -= 1
        elif ry < ay:
            ry += 1
            rem -= 1
        elif rx < ax:
            rx += 1
            rem -= 1
        else:
            break
    return sx * rx, sy * ry, rx + ry


def clamp_move_actions_to_walk_budget(
    actions: list[dict[str, Any]],
    snapshot_json: str,
    grid_feet_per_unit: float = 5.0,
) -> list[dict[str, Any]]:
    """
    Cap [[fas-move-rel]] total Manhattan grid steps to walking speed / grid unit (default 5 ft).
    Matches Foundry module enforcement; keeps outbox actions honest for logs.
    move_abs is not clamped here (needs token position).
    """
    walk = _walk_speed_ft_from_snapshot(snapshot_json)
    if walk is None or walk <= 0 or grid_feet_per_unit <= 0:
        return actions
    max_steps = max(0, int(walk // grid_feet_per_unit))
    remaining = max_steps
    out: list[dict[str, Any]] = []
    for a in actions:
        if a.get("type") != "move_rel":
            out.append(dict(a))
            continue
        dx = int(a.get("dx") or 0)
        dy = int(a.get("dy") or 0)
        if remaining <= 0:
            logger.warning(
                "Dropping move_rel (%s,%s) — walking budget already spent earlier in this batch.",
                dx,
                dy,
            )
            continue
        want = abs(dx) + abs(dy)
        cdx, cdy, used = _clamp_grid_delta_int(dx, dy, remaining)
        if used < want:
            logger.warning(
                "Clamping move_rel (%s,%s) to (%s,%s) — %s/%s steps (walk %.0f ft ≈ %s steps/turn).",
                dx,
                dy,
                cdx,
                cdy,
                used,
                want,
                walk,
                max_steps,
            )
        remaining -= used
        b = dict(a)
        b["dx"] = cdx
        b["dy"] = cdy
        out.append(b)
    return out


# Per combat outbox batch — one “packet” per turn: one move + one attack (with target) + optional equip.
# Damage is rolled automatically by the Foundry module after a hit; do not queue [[fas-damage]] here.
TURN_MAX_EQUIP_ACTIONS = 1
TURN_MAX_ATTACK_ACTIONS = 1
TURN_MAX_SPELL_ACTIONS = 1
# Either one weapon attack OR one offensive spell per batch (not both).
TURN_MAX_COMBINED_ATTACK_AND_SPELL = 1
TURN_MAX_DAMAGE_ITEM_ACTIONS = 0
# One movement instruction total: either [[fas-move-rel]] or [[fas-move-abs]], not several of each.
TURN_MAX_MOVEMENT_DIRECTIVES = 1


def clamp_turn_action_economy(
    actions: list[dict[str, Any]],
    *,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """
    Keep only the first N equip / attack / spell / damage / move directives per outbox batch.
    Applied for every bridge reply (not only when combat snapshot sync is active).
    Rolls pass through unchanged — same order as the model, truncated.
    With defaults above: optional equip, one move, one attack or spell (with target); damage_item dropped (module handles damage).
    """
    if not enabled or not actions:
        return actions
    n_equip = 0
    n_attack = 0
    n_spell = 0
    n_damage = 0
    n_combined = 0
    n_move = 0
    out: list[dict[str, Any]] = []
    for a in actions:
        t = a.get("type")
        if t in ("move_rel", "move_abs"):
            if n_move >= TURN_MAX_MOVEMENT_DIRECTIVES:
                logger.warning(
                    "Clamping movement: keeping first %s move directive(s) per batch (dropped type=%s).",
                    TURN_MAX_MOVEMENT_DIRECTIVES,
                    t,
                )
                continue
            n_move += 1
            out.append(dict(a))
            continue
        if t == "equip_item":
            if n_equip >= TURN_MAX_EQUIP_ACTIONS:
                logger.warning(
                    "Clamping equip_item: keeping first %s per batch (dropped %r).",
                    TURN_MAX_EQUIP_ACTIONS,
                    a.get("itemName"),
                )
                continue
            n_equip += 1
            out.append(dict(a))
            continue
        if t == "attack_item":
            if n_attack >= TURN_MAX_ATTACK_ACTIONS or n_combined >= TURN_MAX_COMBINED_ATTACK_AND_SPELL:
                logger.warning(
                    "Clamping attack_item: attack cap=%s or combined attack+spell cap=%s (dropped %r).",
                    TURN_MAX_ATTACK_ACTIONS,
                    TURN_MAX_COMBINED_ATTACK_AND_SPELL,
                    a.get("itemName"),
                )
                continue
            n_attack += 1
            n_combined += 1
            out.append(dict(a))
            continue
        if t == "spell_item":
            if n_spell >= TURN_MAX_SPELL_ACTIONS or n_combined >= TURN_MAX_COMBINED_ATTACK_AND_SPELL:
                logger.warning(
                    "Clamping spell_item: spell cap=%s or combined cap=%s (dropped %r).",
                    TURN_MAX_SPELL_ACTIONS,
                    TURN_MAX_COMBINED_ATTACK_AND_SPELL,
                    a.get("itemName"),
                )
                continue
            n_spell += 1
            n_combined += 1
            out.append(dict(a))
            continue
        if t == "damage_item":
            if TURN_MAX_DAMAGE_ITEM_ACTIONS <= 0:
                logger.warning(
                    "Dropping damage_item %r — use [[fas-attack]] only; Foundry rolls damage on a hit.",
                    a.get("itemName"),
                )
                continue
            if n_damage >= TURN_MAX_DAMAGE_ITEM_ACTIONS:
                logger.warning(
                    "Clamping damage_item: keeping first %s per batch (dropped %r).",
                    TURN_MAX_DAMAGE_ITEM_ACTIONS,
                    a.get("itemName"),
                )
                continue
            n_damage += 1
            out.append(dict(a))
            continue
        out.append(dict(a))
    return out


def parse_fas_roll_directives(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Backward-compatible: only roll actions (moves still stripped)."""
    clean, actions = parse_fas_directives(text)
    rolls = [
        {"formula": a["formula"], "flavor": a.get("flavor", "")}
        for a in actions
        if a.get("type") == "roll"
    ]
    return clean, rolls


def format_weapon_attack_allowlist(snapshot_json: str) -> str:
    """
    Lists weapon names from the sheet JSON so the model uses [[fas-attack:ExactName]] only.
    Prevents invented items (e.g. Scimitar) when the PC has Longbow/Dagger.
    Emphasizes equipped weapons and requiring an attack on combat turns when in range.
    """
    raw = snapshot_json.strip()
    if not raw:
        return ""
    try:
        import json

        obj = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict):
        return ""
    items = obj.get("items")
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    equipped: list[str] = []
    equipped_ranged: list[str] = []
    equipped_melee: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = (it.get("type") or "").lower()
        if t == "weapon" or (t == "equipment" and it.get("weaponType")):
            n = (it.get("name") or "").strip()
            if not n:
                continue
            if n not in names:
                names.append(n)
            if it.get("equipped") and n not in equipped:
                equipped.append(n)
                st = _weapon_style_class(it)
                if st in ("ranged", "thrown"):
                    equipped_ranged.append(n)
                elif st in ("melee", "reach"):
                    equipped_melee.append(n)
    if not names:
        return ""
    joined = ", ".join(names[:24])
    lines = [
        "**[[fas-attack]] weapon names:** use **only** names from your inventory (exact or partial match). "
        f"Your weapon items include: **{joined}**.",
    ]
    if equipped:
        lines.append(
            "**Equipped weapons** — prefer these for `[[fas-attack:WeaponName|target:...]]` this turn: "
            f"**{', '.join(equipped[:12])}**."
        )
    if equipped_ranged:
        lines.append(
            "**Shoot first when targets are not adjacent:** your equipped **ranged/thrown** weapons are "
            f"**{', '.join(equipped_ranged[:12])}** — use one of these with `[[fas-attack:...|target:...]]` "
            "instead of only moving closer unless you are already in melee or must reposition for line of sight."
        )
    if equipped_melee and equipped_ranged:
        lines.append(
            "**Melee only when appropriate:** "
            f"**{', '.join(equipped_melee[:12])}** — use when foes are adjacent or you have already shot at range."
        )
    lines.append(
        "**One combat packet per reply (server-enforced):** optional **one** `[[fas-equip:...|on]]`, **one** movement "
        "(`[[fas-move-rel]]` **or** `[[fas-move-abs]]`), **one** `[[fas-attack:...|target:...]]` or offensive spell — "
        "**equip + attack on the same turn is intended** (equip line first, then attack; optional move between). "
        "Not multiple attacks or moves. On a **hit**, Foundry rolls **damage** — **never** `[[fas-damage]]`."
    )
    lines.append(
        "**Combat turns:** If a hostile is in range after your **single** move, include **one** attack or spell with `|target:...`. "
        "Compare **battlefield distances** to weapon **range in feet**. Prefer **ranged** when foes are far. "
        "If you omit `|target:`, the server may fill one — still set it explicitly when you can."
    )
    lines.append(
        "Do **not** use weapon or gear names from memory or stereotypes (e.g. **Scimitar**, **Rapier**, **Longsword**) "
        "unless that exact item appears in the lists above — invented names **fail** in Foundry and waste your turn."
    )
    lines.append(
        "Do **not** output attacks with weapons you do not have. "
        "Movement must be `[[fas-move-rel:dx,dy]]` or `[[fas-move-abs:col,row]]` — no other macro syntax."
    )
    return "\n".join(lines)


def format_sheet_snapshot_for_prompt(snapshot_json: str, max_chars: int = 12000) -> str:
    """Pretty-print JSON for system context; truncate if huge."""
    import json

    raw = snapshot_json.strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        out = json.dumps(obj, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        out = raw
    if len(out) > max_chars:
        out = out[: max_chars - 20] + "\n… (truncated)"
    return out
