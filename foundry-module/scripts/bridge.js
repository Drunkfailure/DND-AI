/**
 * Foundry Agent Studio — companion module (player-only bridge).
 * Sends: chat.received, world.connected, actor.sheet, combat.state → desktop HTTP API.
 * Receives: postChatMessage via outbox polling (optional rolls executed after speech).
 */

const MODULE_ID = "foundry-agent-studio";

const sheetTimers = new Map();
let combatTimer = null;

Hooks.once("init", () => {
  game.settings.register(MODULE_ID, "bridgeUrl", {
    name: "Bridge base URL",
    hint: "Must match Foundry Agent Studio (default http://127.0.0.1:17890).",
    scope: "world",
    config: true,
    type: String,
    default: "http://127.0.0.1:17890",
  });
  game.settings.register(MODULE_ID, "bridgeSecret", {
    name: "Bridge secret (X-FAS-Secret)",
    hint: "Copy from the desktop app → Foundry bridge card.",
    scope: "world",
    config: true,
    type: String,
    default: "",
  });
});

function htmlToText(html) {
  const d = document.createElement("div");
  d.innerHTML = html ?? "";
  return d.textContent?.trim() || d.innerText?.trim() || "";
}

function getBridgeConfig() {
  const url = String(game.settings.get(MODULE_ID, "bridgeUrl") ?? "").replace(/\/$/, "");
  const secret = String(game.settings.get(MODULE_ID, "bridgeSecret") ?? "");
  return { url, secret };
}

/** Per-item hints for AI range / targeting (dnd5e). */
function summarizeItemForAi(item) {
  const is = item.system ?? {};
  const t = item.type;
  const row = {
    name: item.name,
    type: t,
  };
  if (is.equipped !== undefined) row.equipped = is.equipped;
  if (is.prepared !== undefined) row.prepared = is.prepared;
  if (t === "weapon") {
    row.range = is.range;
    row.properties = is.properties;
    row.weaponType = is.weaponType;
    if (is.attackBonus !== undefined) row.attackBonus = is.attackBonus;
    if (is.damage?.parts?.length) row.damageFormula = String(is.damage.parts[0]?.[0] ?? "");
  }
  if (t === "equipment" && is.weaponType) {
    row.range = is.range;
    row.properties = is.properties;
    row.weaponType = is.weaponType;
  }
  if (t === "spell") {
    row.level = is.level;
    row.school = is.school;
    row.range = is.range;
    if (is.target) row.target = is.target;
    if (is.duration) row.duration = is.duration;
    if (is.damage?.parts?.length) row.damageFormula = is.damage.parts.map((p) => p[0]).join(" + ");
    if (is.healing) row.healing = is.healing;
  }
  return row;
}

/** JSON-safe snapshot for dnd5e and generic actors (trimmed). */
function serializeActorForBridge(actor) {
  const a = actor?.document ?? actor;
  if (!a) return null;
  const sys = a.system ?? {};
  const out = {
    name: a.name,
    type: a.type,
    id: a.id,
  };
  if (sys.attributes) out.attributes = sys.attributes;
  if (sys.abilities) out.abilities = sys.abilities;
  if (sys.details) out.details = sys.details;
  if (sys.resources) out.resources = sys.resources;
  if (sys.spells) out.spells = sys.spells;
  if (sys.traits) out.traits = sys.traits;
  const items = [];
  const dnd = game.system?.id === "dnd5e";
  try {
    for (const item of a.items?.values?.() ?? []) {
      if (dnd) {
        items.push(summarizeItemForAi(item));
      } else {
        const is = item.system ?? {};
        items.push({
          name: item.name,
          type: item.type,
          quantity: is.quantity,
          equipped: is.equipped,
          prepared: is.prepared,
        });
      }
    }
  } catch {
    /* ignore */
  }
  out.items = items.slice(0, 100);
  if (dnd) {
    out.aiTargetingNote =
      "Items include range/target fields where present. Choose [[fas-attack:Name|target:...]] / " +
      "[[fas-spell:Name|target:...]] using names or actorIds from combat sync; respect reach and spell range.";
  }
  return out;
}

function scheduleActorSheet(actor) {
  const a = actor?.document ?? actor;
  if (!a?.id) return;
  const id = a.id;
  if (sheetTimers.has(id)) clearTimeout(sheetTimers.get(id));
  sheetTimers.set(
    id,
    setTimeout(() => {
      sheetTimers.delete(id);
      void pushActorSheet(a);
    }, 450)
  );
}

function getActorHpSummary(actor) {
  if (!actor) return null;
  const hp = actor.system?.attributes?.hp;
  if (!hp || hp.value === undefined) return null;
  return {
    value: hp.value,
    max: hp.max,
    temp: hp.temp ?? 0,
  };
}

function getActiveCombat() {
  try {
    if (game?.combats?.active) return game.combats.active;
    if (game?.combats?.viewed) return game.combats.viewed;
    if (game?.combat) return game.combat;
  } catch {
    /* ignore */
  }
  return null;
}

function serializeCombatState() {
  const combat = getActiveCombat();
  if (!combat) return null;
  const c = combat.document ?? combat;
  const round = c.round ?? 1;
  const turnIndex = typeof c.turn === "number" ? c.turn : 0;
  const scene = canvas?.scene;
  const sceneName = scene?.name ?? "";
  const order = [];
  const turns = c.turns;
  if (turns) {
    try {
      for (const t of turns) {
        const doc = t.document ?? t;
        const actor = doc.actor;
        order.push({
          id: doc.id,
          name: doc.name || actor?.name || "Unknown",
          initiative: doc.initiative,
          isDefeated: doc.isDefeated,
          actorId: actor?.id ?? null,
          hp: getActorHpSummary(actor),
        });
      }
    } catch {
      const list = turns.contents ?? [];
      for (const t of list) {
        const doc = t.document ?? t;
        const actor = doc.actor;
        order.push({
          id: doc.id,
          name: doc.name || actor?.name || "Unknown",
          initiative: doc.initiative,
          isDefeated: doc.isDefeated,
          actorId: actor?.id ?? null,
          hp: getActorHpSummary(actor),
        });
      }
    }
  }
  while (order.length > 50) order.pop();
  return {
    id: c.id,
    round,
    turnIndex,
    sceneName,
    order,
  };
}

function scheduleCombatPush() {
  if (combatTimer) clearTimeout(combatTimer);
  combatTimer = setTimeout(() => {
    combatTimer = null;
    void pushCombatState();
  }, 400);
}

async function pushCombatState() {
  const { url, secret } = getBridgeConfig();
  if (!secret || !url) return;
  const worldId = game.world?.id ?? "";
  let combat = serializeCombatState();
  let payload = { worldId, combat };
  let body = JSON.stringify({ type: "combat.state", payload });
  if (body.length > 100000 && combat?.order) {
    combat = { ...combat, order: combat.order.slice(0, 12) };
    payload = { worldId, combat };
    body = JSON.stringify({ type: "combat.state", payload });
  }
  await fetch(`${url}/api/bridge/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FAS-Secret": secret },
    body,
  }).catch(() => {});
}

async function pushActorSheet(actor) {
  const { url, secret } = getBridgeConfig();
  if (!secret || !url) return;
  const snapshot = serializeActorForBridge(actor);
  if (!snapshot) return;
  let worldId = game.world?.id ?? "";
  let payload = { actorId: actor.id, worldId, snapshot };
  let body = JSON.stringify({ type: "actor.sheet", payload });
  if (body.length > 52000) {
    snapshot.items = (snapshot.items || []).slice(0, 25);
    payload = { actorId: actor.id, worldId, snapshot };
    body = JSON.stringify({ type: "actor.sheet", payload });
  }
  if (body.length > 52000) {
    payload = {
      actorId: actor.id,
      worldId,
      snapshot: {
        name: snapshot.name,
        id: snapshot.id,
        note: "Snapshot truncated — reduce inventory or disable heavy modules.",
      },
    };
    body = JSON.stringify({ type: "actor.sheet", payload });
  }
  await fetch(`${url}/api/bridge/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FAS-Secret": secret },
    body,
  }).catch(() => {});
}

Hooks.once("ready", () => {
  const { url, secret } = getBridgeConfig();
  if (!secret) {
    console.warn("[Foundry Agent Studio] Set bridge secret in Module Settings.");
    return;
  }

  fetch(`${url}/api/bridge/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FAS-Secret": secret },
    body: JSON.stringify({
      type: "world.connected",
      payload: { worldId: game.world?.id ?? "" },
    }),
  }).catch(() => {});

  setInterval(() => {
    void pollOutbox(url, secret);
  }, 500);

  Hooks.on("updateActor", (doc) => {
    const actor = doc?.document ?? doc;
    if (actor) scheduleActorSheet(actor);
  });

  Hooks.on("createActor", (doc) => {
    const actor = doc?.document ?? doc;
    if (actor) scheduleActorSheet(actor);
  });

  setTimeout(() => {
    try {
      for (const a of game.actors ?? []) {
        if (a.type === "character") scheduleActorSheet(a);
      }
    } catch {
      /* ignore */
    }
  }, 2500);

  setTimeout(() => scheduleCombatPush(), 1800);

  Hooks.on("updateCombat", () => scheduleCombatPush());
  Hooks.on("deleteCombat", () => scheduleCombatPush());
  Hooks.on("createCombat", () => scheduleCombatPush());
  Hooks.on("updateCombatant", () => scheduleCombatPush());
});

Hooks.on("createChatMessage", (message, _options, _userId) => {
  const { url, secret } = getBridgeConfig();
  if (!secret || !url) return;

  const msg = message?.document ?? message;
  const authorId = msg.author?.id ?? "";
  const actorId = msg.speaker?.actor?.id ?? "";
  const worldId = game.world?.id ?? "";
  const content = htmlToText(msg.content);

  fetch(`${url}/api/bridge/event`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-FAS-Secret": secret },
    body: JSON.stringify({
      type: "chat.received",
      payload: {
        messageId: msg.id,
        userId: authorId,
        actorId,
        worldId,
        content,
      },
    }),
  }).catch(() => {});
});

async function pollOutbox(url, secret) {
  try {
    const r = await fetch(`${url}/api/bridge/outbox`, {
      headers: { "X-FAS-Secret": secret },
    });
    if (!r.ok) return;
    const j = await r.json();
    const items = j.items ?? [];
    for (const item of items) {
      await postChatMessage(item);
    }
  } catch {
    /* Desktop offline */
  }
}

function legacyRollsToActions(rolls) {
  if (!rolls?.length) return [];
  return rolls.map((r) => ({
    type: "roll",
    formula: r.formula,
    flavor: r.flavor ?? "",
  }));
}

function findTokenForActor(actorId) {
  if (!canvas?.ready || !actorId) return null;
  const placeables = canvas.tokens?.placeables ?? [];
  const matches = placeables.filter((t) => t.actor?.id === actorId);
  if (!matches.length) return null;
  return matches.find((t) => t.controlled) ?? matches[0];
}

function gridCellCenterPixels(gx, gy) {
  const grid = canvas.grid;
  const s = grid.size;
  const offset = { i: gy, j: gx };
  try {
    if (typeof grid.getCenterPoint === "function") {
      const p = grid.getCenterPoint(offset);
      if (p && typeof p.x === "number" && typeof p.y === "number") return { x: p.x, y: p.y };
    }
  } catch {
    /* fall through */
  }
  try {
    if (typeof grid.getTopLeftPoint === "function") {
      const tl = grid.getTopLeftPoint(offset);
      return { x: tl.x + s / 2, y: tl.y + s / 2 };
    }
  } catch {
    /* fall through */
  }
  const pad = canvas.dimensions?.padding ?? 0;
  return { x: pad + gx * s + s / 2, y: pad + gy * s + s / 2 };
}

async function executeMoveRel(actorId, dx, dy) {
  if (!canvas?.ready) {
    console.warn("[Foundry Agent Studio] No active canvas — move skipped.");
    return;
  }
  const token = findTokenForActor(actorId);
  if (!token) {
    console.warn("[Foundry Agent Studio] No token for actor on this scene:", actorId);
    return;
  }
  const grid = canvas.grid;
  const s = grid.size || 100;
  if (grid.isGridless) {
    await token.document.update({ x: token.x + dx * s, y: token.y + dy * s });
    return;
  }
  try {
    if (typeof grid.getOffset === "function" && typeof grid.getCenterPoint === "function") {
      const curOff = grid.getOffset({ x: token.x, y: token.y });
      const nextOff = { i: curOff.i + dy, j: curOff.j + dx };
      const p = grid.getCenterPoint(nextOff);
      await token.document.update({ x: p.x, y: p.y });
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] Grid move_rel fallback:", e);
  }
  await token.document.update({ x: token.x + dx * s, y: token.y + dy * s });
}

async function executeMoveAbs(actorId, gx, gy) {
  if (!canvas?.ready) {
    console.warn("[Foundry Agent Studio] No active canvas — move skipped.");
    return;
  }
  const token = findTokenForActor(actorId);
  if (!token) {
    console.warn("[Foundry Agent Studio] No token for actor on this scene:", actorId);
    return;
  }
  const grid = canvas.grid;
  if (grid.isGridless) {
    console.warn("[Foundry Agent Studio] Gridless scene — fas-move-abs skipped.");
    return;
  }
  const p = gridCellCenterPixels(gx, gy);
  await token.document.update({ x: p.x, y: p.y });
}

async function executeRoll(chatSpeaker, spec) {
  const RollImpl = globalThis.Roll;
  if (!RollImpl) return;
  const formula = spec.formula;
  if (!formula) return;
  const flavor = spec.flavor || "";
  try {
    const roll = new RollImpl(formula);
    await roll.evaluate({ async: true });
    await roll.toMessage({
      speaker: chatSpeaker ?? ChatMessage.getSpeaker({ user: game.user }),
      flavor: flavor || undefined,
    });
  } catch (e) {
    console.warn("[Foundry Agent Studio] Roll failed:", formula, e);
  }
}

function findItemByName(actor, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q || !actor?.items) return null;
  const items = [...actor.items.values()];
  let it = items.find((i) => (i.name || "").toLowerCase() === q);
  if (it) return it;
  it = items.find((i) => (i.name || "").toLowerCase().includes(q));
  if (it) return it;
  const compact = q.replace(/\s+/g, "");
  it = items.find((i) => (i.name || "").toLowerCase().replace(/\s+/g, "") === compact);
  return it ?? null;
}

function looksLikeActorId(s) {
  const t = (s || "").trim();
  return t.length >= 8 && /^[a-z0-9]+$/i.test(t);
}

/** Resolve a token on the active scene for targeting (name or actor id). */
function resolveTargetToken(attackerActorId, query) {
  if (!query?.trim() || !canvas?.ready) return null;
  const q = query.trim();
  const tokens = canvas.tokens?.placeables ?? [];
  if (looksLikeActorId(q)) {
    const byActor = tokens.find((t) => t.actor?.id === q);
    if (byActor) return byActor;
  }
  const ql = q.toLowerCase();
  const candidates = tokens.filter((t) => {
    if (t.actor?.id === attackerActorId) return false;
    const an = (t.actor?.name || "").toLowerCase();
    const tn = (t.name || "").toLowerCase();
    return an === ql || tn === ql || an.includes(ql) || tn.includes(ql);
  });
  return candidates[0] ?? null;
}

function applyTargetSelection(targetToken) {
  if (!targetToken) return;
  try {
    targetToken.setTarget(true, { releaseOthers: true });
  } catch (e) {
    console.warn("[Foundry Agent Studio] setTarget:", e);
  }
}

function logDistanceToTarget(attackerActorId, targetToken) {
  try {
    const from = findTokenForActor(attackerActorId);
    if (!from || !targetToken || typeof canvas?.grid?.measurePath !== "function") return;
    const r = canvas.grid.measurePath([from, targetToken]);
    if (r?.distance != null) {
      console.log("[Foundry Agent Studio] grid distance to target:", r.distance);
    }
  } catch {
    /* ignore */
  }
}

function prepareTarget(attackerActorId, targetQuery) {
  if (!targetQuery?.trim()) return null;
  const token = resolveTargetToken(attackerActorId, targetQuery.trim());
  if (!token) {
    console.warn("[Foundry Agent Studio] target not found on scene:", targetQuery);
    return null;
  }
  applyTargetSelection(token);
  logDistanceToTarget(attackerActorId, token);
  return token;
}

async function executeItemAttack(actorId, itemName, targetQuery) {
  const actor = game.actors.get(actorId);
  if (!actor) {
    console.warn("[Foundry Agent Studio] fas-attack: no actor", actorId);
    return;
  }
  if (game.system?.id !== "dnd5e") {
    console.warn("[Foundry Agent Studio] fas-attack requires system dnd5e (got " + (game.system?.id ?? "?") + ")");
    return;
  }
  const item = findItemByName(actor, itemName);
  if (!item) {
    console.warn("[Foundry Agent Studio] fas-attack: item not found:", itemName);
    return;
  }
  const targetToken = prepareTarget(actorId, targetQuery);
  const opts = { fastForward: true, chatMessage: true };
  if (targetToken) {
    opts.event = { target: targetToken };
  }
  try {
    if (typeof item.rollAttack === "function") {
      await item.rollAttack(opts);
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] rollAttack:", e);
  }
  try {
    if (typeof item.use === "function") {
      await item.use({}, { configureDialog: false });
    }
  } catch (e2) {
    console.warn("[Foundry Agent Studio] fas-attack fallback:", e2);
  }
}

async function executeItemSpell(actorId, itemName, targetQuery) {
  const actor = game.actors.get(actorId);
  if (!actor) {
    console.warn("[Foundry Agent Studio] fas-spell: no actor", actorId);
    return;
  }
  if (game.system?.id !== "dnd5e") {
    console.warn("[Foundry Agent Studio] fas-spell requires system dnd5e");
    return;
  }
  const item = findItemByName(actor, itemName);
  if (!item) {
    console.warn("[Foundry Agent Studio] fas-spell: item not found:", itemName);
    return;
  }
  if (item.type !== "spell") {
    console.warn("[Foundry Agent Studio] fas-spell: not a spell item:", itemName, item.type);
  }
  const targetToken = prepareTarget(actorId, targetQuery);
  const opts = { fastForward: true, chatMessage: true };
  if (targetToken) opts.event = { target: targetToken };
  try {
    if (typeof item.rollAttack === "function") {
      await item.rollAttack(opts);
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] spell rollAttack:", e);
  }
  try {
    if (typeof item.rollDamage === "function") {
      await item.rollDamage({ fastForward: true, critical: false });
      return;
    }
  } catch (e2) {
    console.warn("[Foundry Agent Studio] spell rollDamage:", e2);
  }
  try {
    if (typeof item.use === "function") {
      await item.use({}, { configureDialog: false });
    }
  } catch (e3) {
    console.warn("[Foundry Agent Studio] fas-spell use:", e3);
  }
}

async function executeItemDamage(actorId, itemName, critical, targetQuery) {
  const actor = game.actors.get(actorId);
  if (!actor) {
    console.warn("[Foundry Agent Studio] fas-damage: no actor", actorId);
    return;
  }
  if (game.system?.id !== "dnd5e") {
    console.warn("[Foundry Agent Studio] fas-damage requires system dnd5e");
    return;
  }
  const item = findItemByName(actor, itemName);
  if (!item) {
    console.warn("[Foundry Agent Studio] fas-damage: item not found:", itemName);
    return;
  }
  prepareTarget(actorId, targetQuery);
  const crit = !!critical;
  try {
    if (typeof item.rollDamage === "function") {
      await item.rollDamage({ fastForward: true, critical: crit });
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] rollDamage:", e);
  }
  try {
    if (typeof item.rollDamage === "function") {
      await item.rollDamage({ critical: crit });
    }
  } catch (e2) {
    console.warn("[Foundry Agent Studio] fas-damage retry:", e2);
  }
}

async function executeOutboxActions(item, chatSpeaker) {
  let actions = item.actions;
  if (!actions?.length && item.rolls?.length) {
    actions = legacyRollsToActions(item.rolls);
  }
  if (!actions?.length) return;

  const actorId = item.actorId;
  for (const action of actions) {
    const t = action?.type;
    if (t === "roll") {
      await executeRoll(chatSpeaker, action);
    } else if (t === "move_rel" && actorId) {
      await executeMoveRel(actorId, action.dx ?? 0, action.dy ?? 0);
    } else if (t === "move_abs" && actorId) {
      await executeMoveAbs(actorId, action.gx ?? 0, action.gy ?? 0);
    } else if (t === "attack_item" && actorId) {
      await executeItemAttack(actorId, action.itemName ?? "", action.targetQuery);
    } else if (t === "spell_item" && actorId) {
      await executeItemSpell(actorId, action.itemName ?? "", action.targetQuery);
    } else if (t === "damage_item" && actorId) {
      await executeItemDamage(actorId, action.itemName ?? "", action.critical, action.targetQuery);
    }
  }
}

async function postChatMessage(item) {
  const data = { user: item.userId, content: item.content };
  let chatSpeaker = null;
  if (item.actorId) {
    const actor = game.actors.get(item.actorId);
    if (actor) {
      data.speaker = ChatMessage.getSpeaker({ actor });
      chatSpeaker = data.speaker;
    }
  }
  if (!chatSpeaker && item.userId) {
    const u = game.users.get(item.userId);
    if (u) {
      data.speaker = ChatMessage.getSpeaker({ user: u });
      chatSpeaker = data.speaker;
    }
  }
  await ChatMessage.create(data);
  await executeOutboxActions(item, chatSpeaker);
}
