"""Parse Foundry VTT directives in agent replies: rolls, token moves (ordered)."""

from __future__ import annotations

import re
from typing import Any

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
    actions = [a for _, _, a in merged]
    return out, actions


def parse_fas_roll_directives(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Backward-compatible: only roll actions (moves still stripped)."""
    clean, actions = parse_fas_directives(text)
    rolls = [
        {"formula": a["formula"], "flavor": a.get("flavor", "")}
        for a in actions
        if a.get("type") == "roll"
    ]
    return clean, rolls


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
