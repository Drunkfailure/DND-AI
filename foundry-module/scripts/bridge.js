/**
 * Foundry Agent Studio — companion module (player-only bridge).
 * Sends: chat.received, world.connected, actor.sheet, combat.state → desktop HTTP API.
 * Receives: postChatMessage via outbox polling (optional rolls executed after speech).
 */

const MODULE_ID = "foundry-agent-studio";

const sheetTimers = new Map();
let combatTimer = null;
/** When outbox polling fails repeatedly, GM gets one warning (connection = chat delivery + moves + initiative list). */
let bridgeOutboxFailStreak = 0;
let bridgeUnreachableWarned = false;

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
    id: item.id,
    name: item.name,
    type: t,
  };
  if (is.equipped !== undefined) row.equipped = is.equipped;
  if (is.prepared !== undefined) row.prepared = is.prepared;
  if (is.quantity !== undefined) row.quantity = is.quantity;
  if (is.weight !== undefined) row.weight = is.weight;
  if (is.rarity) row.rarity = is.rarity;
  if (is.attunement !== undefined) row.attuned = is.attunement;
  const descHtml = is.description?.value ?? is.description ?? "";
  if (descHtml) {
    const plain = htmlToText(String(descHtml));
    if (plain) {
      row.description = plain.length > 280 ? `${plain.slice(0, 277)}…` : plain;
    }
  }
  if (t === "weapon") {
    row.range = is.range;
    row.properties = is.properties;
    row.weaponType = is.weaponType;
    if (is.attackBonus !== undefined) row.attackBonus = is.attackBonus;
    if (is.damage?.parts?.length) row.damageFormula = String(is.damage.parts[0]?.[0] ?? "");
  }
  if (t === "equipment") {
    if (is.armor) row.armor = { value: is.armor?.value, dex: is.armor?.dex };
    if (is.type?.value) row.equipmentType = is.type.value;
    if (is.weaponType) {
      row.range = is.range;
      row.properties = is.properties;
      row.weaponType = is.weaponType;
    }
  }
  if (t === "consumable") {
    if (is.uses) row.uses = is.uses;
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
  if (t === "loot") {
    if (is.weaponType) row.weaponType = is.weaponType;
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
  if (dnd && items.length) {
    items.sort((a, b) => {
      const rank = (x) => {
        const t = x.type;
        if (t === "weapon") return 0;
        if (t === "equipment") return 1;
        if (t === "consumable") return 2;
        if (t === "spell") return 3;
        return 4;
      };
      return rank(a) - rank(b);
    });
  }
  out.items = items.slice(0, 120);
  if (dnd) {
    out.aiTargetingNote =
      "Inventory `items` are weapons first, then armor/gear (equipped, range, descriptions). " +
      "Automation must be exact [[fas-...]] lines only — plain text or // comments do not run. " +
      "[[fas-attack:ItemName]] with no target picks the nearest **hostile** token; the module then rolls **damage** automatically "
      "(critical damage if the attack was a crit). " +
      "Ranged weapons use **range in feet** — you need not be adjacent. " +
      "[[fas-equip:Item|on]] / [[fas-equip:Item|off]] to equip. " +
      "Named targets: [[fas-attack:YourWeapon|target:Goblin]] or |target:actorId from combat sync.";
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

/** Foundry actor ids for enabled Agent Studio player agents (from desktop bridge). */
let playerActorIdSet = new Set();
/** Dedupe combat-turn → LLM prompts when multiple hooks fire for the same step. */
let lastCombatTurnPromptKey = "";

async function refreshPlayerActorIds() {
  const { url, secret } = getBridgeConfig();
  if (!url || !secret) return;
  try {
    const wid = game.world?.id ?? "";
    const qs = new URLSearchParams({ worldId: wid });
    const r = await fetch(`${url}/api/bridge/player-actor-ids?${qs}`, {
      headers: { "X-FAS-Secret": secret },
    });
    if (!r.ok) return;
    const j = await r.json();
    const ids = j.actorIds;
    if (Array.isArray(ids)) {
      playerActorIdSet = new Set(ids.filter(Boolean));
    }
  } catch {
    /* desktop offline */
  }
}

/** Actor id for a combatant (linked token / character sheet). */
function combatantLinkedActorId(combatant) {
  const doc = combatant?.document ?? combatant;
  if (!doc) return null;
  try {
    if (doc.actor?.id) return doc.actor.id;
    if (doc.actorId) return doc.actorId;
    const tok = doc.token?.document ?? doc.token;
    if (tok?.actor?.id) return tok.actor.id;
    if (tok?.actorId) return tok.actorId;
  } catch {
    /* ignore */
  }
  return null;
}

function canUserRollInitiativeForActor(actor) {
  if (game.user?.isGM) return true;
  if (!actor) return false;
  try {
    return !!actor.isOwner;
  } catch {
    return false;
  }
}

/**
 * When combat starts, roll initiative for combatants tied to Agent Studio–linked character sheets.
 * GM can roll for any linked combatant; a player client rolls only for actors they own.
 */
async function maybeRollAiPlayerInitiative(combatant) {
  const doc = combatant?.document ?? combatant;
  if (!doc) return;
  const aid = combatantLinkedActorId(combatant);
  if (!aid) return;
  const actor = doc.actor ?? game.actors?.get(aid);
  if (!canUserRollInitiativeForActor(actor)) return;
  if (!playerActorIdSet.size) {
    await refreshPlayerActorIds();
  }
  if (!playerActorIdSet.has(aid)) return;
  if (doc.initiative !== null && doc.initiative !== undefined) return;

  const combat = doc.combat;
  if (!combat) return;

  try {
    if (typeof combat.rollInitiative === "function") {
      await combat.rollInitiative([doc.id], { messageOptions: {} });
    } else if (typeof doc.rollInitiative === "function") {
      await doc.rollInitiative({});
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] initiative roll failed:", e);
  }
}

function scheduleSweepAiInitiative(combat) {
  const c = combat?.document ?? combat;
  if (!c) return;
  setTimeout(() => {
    void (async () => {
      const turns = c.turns;
      if (!turns) return;
      let list;
      try {
        list = turns.contents ?? turns;
      } catch {
        return;
      }
      try {
        for (const t of list) {
          await maybeRollAiPlayerInitiative(t);
        }
      } catch {
        /* ignore */
      }
    })();
  }, 200);
}

/**
 * When the initiative tracker advances to a linked PC, ask Agent Studio to take the turn (chat + fas-* actions).
 * GM client only; delayed so combat.state sync can land before the model runs.
 */
function postCombatTurnIfAiPlayer(combat, combatant) {
  if (!game.user?.isGM) return;
  const { url, secret } = getBridgeConfig();
  if (!url || !secret) return;
  const c = combat?.document ?? combat;
  const doc = combatant?.document ?? combatant;
  const aid = combatantLinkedActorId(doc);
  if (!aid) return;
  const key = `${c.id ?? ""}:${c.round ?? 0}:${c.turn ?? 0}:${aid}`;
  if (key === lastCombatTurnPromptKey) return;
  lastCombatTurnPromptKey = key;
  setTimeout(() => {
    void (async () => {
      await refreshPlayerActorIds();
      if (!playerActorIdSet.has(aid)) return;
      try {
        await fetch(`${url}/api/bridge/event`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-FAS-Secret": secret },
          body: JSON.stringify({
            type: "combat.turn",
            payload: { worldId: game.world?.id ?? "", actorId: aid },
          }),
        });
      } catch {
        /* bridge offline */
      }
    })();
  }, 450);
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
          disposition: dispositionLabel(doc.disposition),
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
          disposition: dispositionLabel(doc.disposition),
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

function dispositionLabel(d) {
  if (d === -1 || d === CONST?.TOKEN?.DISPOSITIONS?.HOSTILE) return "hostile";
  if (d === 1 || d === CONST?.TOKEN?.DISPOSITIONS?.FRIENDLY) return "friendly";
  return "neutral";
}

/**
 * Tactical layout for AI prompts: scene grid, combatant token positions, wall segments.
 * Omitted hidden tokens; fog/LOS not fully modeled (GM is authority).
 */
function serializeBattlefield(combatSerialized) {
  if (!canvas?.ready || !combatSerialized) return null;
  const scene = canvas.scene;
  if (!scene) return null;
  const grid = canvas.grid;
  const order = combatSerialized.order || [];
  const actorIds = [...new Set(order.map((o) => o.actorId).filter(Boolean))];
  if (actorIds.length === 0) return null;

  const gridless = !!grid.isGridless;
  const gridSize = grid.size || 100;
  const padding = canvas.dimensions?.padding ?? 0;
  const tokens = [];
  const missingFromMap = [];

  for (const aid of actorIds) {
    const token = findTokenForActor(aid);
    const entry = order.find((o) => o.actorId === aid);
    const displayName = entry?.name || "?";
    if (!token) {
      missingFromMap.push({ name: displayName, actorId: aid, reason: "no_token_on_scene" });
      continue;
    }
    const doc = token.document;
    if (doc?.hidden) {
      missingFromMap.push({ name: displayName, actorId: aid, reason: "gm_hidden" });
      continue;
    }
    let position;
    if (gridless) {
      position = { gridless: true, pixelX: Math.round(token.x), pixelY: Math.round(token.y) };
    } else {
      try {
        if (typeof grid.getOffset === "function") {
          const off = grid.getOffset({ x: token.x, y: token.y });
          position = { col: off.j, row: off.i };
        } else {
          position = {
            approxCol: (token.x - padding) / gridSize,
            approxRow: (token.y - padding) / gridSize,
          };
        }
      } catch {
        position = {
          approxCol: (token.x - padding) / gridSize,
          approxRow: (token.y - padding) / gridSize,
        };
      }
    }
    tokens.push({
      name: displayName,
      actorId: aid,
      disposition: dispositionLabel(doc.disposition),
      position,
      width: doc.width ?? 1,
      height: doc.height ?? 1,
      elevation: doc.elevation ?? 0,
    });
  }

  const MAX_WALL_SEGMENTS = 450;
  const walls = [];
  const wallPlaceables = canvas.walls?.placeables ?? [];
  for (const w of wallPlaceables) {
    if (walls.length >= MAX_WALL_SEGMENTS) break;
    const d = w.document;
    if (!d?.coords) continue;
    const arr = Array.from(d.coords);
    if (arr.length < 4) continue;
    const door =
      typeof d.door === "number" && d.door > 0 ? { door: d.door, ds: d.ds } : null;
    for (let k = 0; k + 3 < arr.length && walls.length < MAX_WALL_SEGMENTS; k += 4) {
      const seg = {
        ax: Math.round(arr[k] * 10) / 10,
        ay: Math.round(arr[k + 1] * 10) / 10,
        bx: Math.round(arr[k + 2] * 10) / 10,
        by: Math.round(arr[k + 3] * 10) / 10,
      };
      if (!gridless && typeof grid.getOffset === "function") {
        try {
          const A = grid.getOffset({ x: arr[k], y: arr[k + 1] });
          const B = grid.getOffset({ x: arr[k + 2], y: arr[k + 3] });
          seg.grid = { colA: A.j, rowA: A.i, colB: B.j, rowB: B.i };
        } catch {
          /* keep pixels only */
        }
      }
      if (door && k === 0) seg.door = door;
      walls.push(seg);
    }
  }

  /** Per-actor grid distance to every other combatant (tactical targeting). Uses measurePath when available. */
  const distancesFromActors = {};
  try {
    for (const aid of actorIds) {
      const tSelf = findTokenForActor(aid);
      if (!tSelf) continue;
      const rows = [];
      for (const oid of actorIds) {
        if (oid === aid) continue;
        const tOther = findTokenForActor(oid);
        if (!tOther) continue;
        const d = tokenDistanceOnGrid(tSelf, tOther);
        const entry = order.find((o) => o.actorId === oid);
        rows.push({
          actorId: oid,
          name: entry?.name || "?",
          disposition: dispositionLabel(tOther.document?.disposition),
          gridDistance: Math.round(d * 100) / 100,
          hp: entry?.hp ?? null,
          defeated: !!entry?.isDefeated,
        });
      }
      rows.sort((a, b) => (a.gridDistance ?? 0) - (b.gridDistance ?? 0));
      distancesFromActors[aid] = rows;
    }
  } catch {
    /* ignore */
  }

  return {
    sceneId: scene.id,
    sceneName: scene.name ?? "",
    gridless,
    gridSizePixels: gridSize,
    sceneGridSize: { columns: scene.width ?? null, rows: scene.height ?? null },
    coordinateNote:
      "Token `col`/`row` are 0-based grid indices; `[[fas-move-abs:col,row]]` uses the same column then row.",
    visibilityNote:
      "Built from the Foundry client running this module (often the GM). Hidden tokens are omitted. Fog of war and line-of-sight are not computed — trust the GM for what your PC can see.",
    tokens,
    distancesFromActors,
    missingFromMap,
    walls,
    wallSegmentCount: walls.length,
    wallObjectsOnScene: wallPlaceables.length,
    wallsTruncated: walls.length >= MAX_WALL_SEGMENTS && wallPlaceables.length > 0,
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
  let battlefield = combat ? serializeBattlefield(combat) : null;
  let payload = { worldId, combat, battlefield };

  const buildBody = () => JSON.stringify({ type: "combat.state", payload });
  let body = buildBody();
  while (body.length > 100000 && battlefield?.walls?.length > 20) {
    const w = battlefield.walls;
    battlefield = {
      ...battlefield,
      walls: w.slice(0, Math.max(20, Math.floor(w.length * 0.65))),
      wallsTruncated: true,
    };
    payload = { worldId, combat, battlefield };
    body = buildBody();
  }
  if (body.length > 100000 && battlefield) {
    const { walls: _w, ...rest } = battlefield;
    battlefield = { ...rest, walls: [], wallsOmitted: true, visibilityNote: battlefield.visibilityNote };
    payload = { worldId, combat, battlefield };
    body = buildBody();
  }
  if (body.length > 100000 && combat?.order) {
    combat = { ...combat, order: combat.order.slice(0, 12) };
    payload = { worldId, combat, battlefield };
    body = buildBody();
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

  void refreshPlayerActorIds();
  setInterval(() => {
    void refreshPlayerActorIds();
  }, 120000);

  Hooks.on("createCombatant", (cm) => {
    void maybeRollAiPlayerInitiative(cm);
  });
  Hooks.on("createCombat", (doc) => {
    scheduleSweepAiInitiative(doc);
    scheduleCombatPush();
  });
  /** Fires when the GM clicks Begin Encounter — combatants already exist; sweep for linked sheets without initiative. */
  Hooks.on("combatStart", (combat, _updateData) => {
    scheduleSweepAiInitiative(combat);
  });

  Hooks.on("combatTurn", (combat, combatant, _options) => {
    scheduleCombatPush();
    postCombatTurnIfAiPlayer(combat, combatant);
  });

  Hooks.on("updateCombat", (combat, changed) => {
    scheduleCombatPush();
    if (changed && (changed.turn !== undefined || changed.round !== undefined)) {
      const c = combat?.document ?? combat;
      const turns = c?.turns;
      if (!turns) return;
      let arr;
      try {
        arr = [...(turns.contents ?? turns)];
      } catch {
        return;
      }
      const ti = typeof c.turn === "number" ? c.turn : 0;
      if (ti >= 0 && ti < arr.length) {
        postCombatTurnIfAiPlayer(c, arr[ti]);
      }
    }
  });
  Hooks.on("deleteCombat", () => scheduleCombatPush());
  Hooks.on("updateCombatant", () => scheduleCombatPush());

  const combatTouch = () => {
    if (getActiveCombat()) scheduleCombatPush();
  };
  Hooks.on("updateToken", combatTouch);
  Hooks.on("createToken", combatTouch);
  Hooks.on("deleteToken", combatTouch);
  Hooks.on("updateWall", combatTouch);
  Hooks.on("createWall", combatTouch);
  Hooks.on("deleteWall", combatTouch);
});

/**
 * Attack/damage rolls (and other roll chat cards) must not trigger chat.received — that would invoke the
 * desktop agent again and cause an attack → roll message → AI reply → attack loop.
 * Messages created by our own outbox must also be skipped or they echo back as a second prompt.
 */
function shouldForwardChatToBridge(msg) {
  if (!msg) return false;
  try {
    if (msg.flags?.[MODULE_ID]?.fromOutbox) return false;
  } catch {
    /* ignore */
  }
  /** Midi QOL workflow / merged cards — often have no `msg.rolls` at hook time, so they used to pass through and spam chat.received → AI → more attacks. */
  try {
    if (msg.flags?.["midi-qol"] != null) return false;
  } catch {
    /* ignore */
  }
  /** dnd5e attack/damage/save/item activity cards (Midi and core rolls). */
  try {
    const d5 = msg.flags?.dnd5e;
    if (d5 && (d5.roll != null || d5.item != null || d5.activity != null)) return false;
  } catch {
    /* ignore */
  }
  let rollCount = 0;
  try {
    const rolls = msg.rolls;
    if (rolls?.size != null) rollCount = rolls.size;
    else if (Array.isArray(rolls)) rollCount = rolls.length;
  } catch {
    /* ignore */
  }
  if (rollCount > 0) return false;
  try {
    if (typeof CONST !== "undefined" && CONST.CHAT_MESSAGE_TYPES && msg.type === CONST.CHAT_MESSAGE_TYPES.ROLL) {
      return false;
    }
  } catch {
    /* ignore */
  }
  return true;
}

Hooks.on("createChatMessage", (message, _options, _userId) => {
  const { url, secret } = getBridgeConfig();
  if (!secret || !url) return;

  const msg = message?.document ?? message;
  if (!shouldForwardChatToBridge(msg)) return;

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

function notifyBridgeUnreachable(url) {
  if (!game.user?.isGM || bridgeUnreachableWarned) return;
  bridgeUnreachableWarned = true;
  const msg =
    `Foundry Agent Studio: no connection to ${url}. Start the desktop app on this machine. ` +
    `In Configure Settings → Core, set Bridge port to match Module Settings → Bridge base URL here (same host and port). ` +
    `Until this works: AI chat may not arrive, moves/attacks will not run, and auto-initiative will not fire.`;
  try {
    ui.notifications?.warn(msg, { permanent: false });
  } catch {
    /* ignore */
  }
  console.warn("[Foundry Agent Studio]", msg);
}

async function pollOutbox(url, secret) {
  try {
    const r = await fetch(`${url}/api/bridge/outbox`, {
      headers: { "X-FAS-Secret": secret },
    });
    if (!r.ok) {
      bridgeOutboxFailStreak++;
      if (bridgeOutboxFailStreak >= 4) notifyBridgeUnreachable(url);
      return;
    }
    bridgeOutboxFailStreak = 0;
    if (bridgeUnreachableWarned) {
      bridgeUnreachableWarned = false;
      try {
        if (game.user?.isGM) {
          ui.notifications?.info(`Foundry Agent Studio: bridge connected (${url}).`, { permanent: false });
        }
      } catch {
        /* ignore */
      }
    }
    const j = await r.json();
    const items = j.items ?? [];
    for (const item of items) {
      await postChatMessage(item);
    }
  } catch {
    bridgeOutboxFailStreak++;
    if (bridgeOutboxFailStreak >= 4) notifyBridgeUnreachable(url);
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

function tokenDistanceOnGrid(fromToken, toToken) {
  if (!fromToken || !toToken) return Infinity;
  try {
    if (typeof canvas?.grid?.measurePath === "function") {
      const r = canvas.grid.measurePath([fromToken, toToken]);
      if (r && typeof r.distance === "number") return r.distance;
      if (r && typeof r.spaces === "number") return r.spaces;
    }
  } catch {
    /* ignore */
  }
  const gs = canvas?.grid?.size || 100;
  const dx = fromToken.x - toToken.x;
  const dy = fromToken.y - toToken.y;
  return Math.sqrt(dx * dx + dy * dy) / gs;
}

function isHostileToken(token) {
  const d = token?.document?.disposition;
  const HOSTILE = CONST?.TOKEN?.DISPOSITIONS?.HOSTILE ?? -1;
  return d === HOSTILE;
}

/**
 * When a heal targets 0 HP, only non-hostile tokens (allies / neutrals) — not defeated enemies.
 * Conscious enemies could still receive healing in edge cases; we only gate **down** targets here.
 */
function healingMayIncludeDefeatedToken(token) {
  if (!token?.actor || !isActorDefeated(token.actor)) return true;
  if (isHostileToken(token)) {
    console.warn(
      "[Foundry Agent Studio] Healing cannot target defeated hostile enemies — use allies (non-hostile disposition) when down."
    );
    return false;
  }
  return true;
}

/** Closest non-hidden hostile token (for [[fas-attack:...]] with no explicit target). */
function resolveNearestHostileToken(attackerActorId) {
  if (!canvas?.ready) return null;
  const self = findTokenForActor(attackerActorId);
  if (!self) return null;
  const placeables = canvas.tokens?.placeables ?? [];
  let best = null;
  let bestD = Infinity;
  for (const t of placeables) {
    if (!t.actor?.id || t.actor.id === attackerActorId) continue;
    if (t.document?.hidden) continue;
    if (!isHostileToken(t)) continue;
    if (isActorDefeated(t.actor)) continue;
    const d = tokenDistanceOnGrid(self, t);
    if (d < bestD) {
      bestD = d;
      best = t;
    }
  }
  if (best) {
    applyTargetSelection(best);
    logDistanceToTarget(attackerActorId, best);
  } else {
    console.warn("[Foundry Agent Studio] no hostile token on scene for nearest-target attack");
  }
  return best;
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

/** Scene distance in feet per grid unit (square); defaults to 5. */
function getSceneGridDistanceFeet() {
  try {
    const g = canvas?.scene?.grid;
    if (g && typeof g.distance === "number" && g.distance > 0) return g.distance;
    const d = canvas?.dimensions?.distance;
    if (typeof d === "number" && d > 0) return d;
  } catch {
    /* ignore */
  }
  return 5;
}

/** Walking speed in feet (dnd5e + loose fallbacks). */
function getActorWalkSpeedFeet(actor) {
  if (!actor) return null;
  try {
    const mov = actor.system?.attributes?.movement;
    if (mov && typeof mov.walk === "number" && mov.walk >= 0) return mov.walk;
    if (mov && mov.walk !== undefined && mov.walk !== "") {
      const w = parseFloat(String(mov.walk));
      if (!Number.isNaN(w) && w >= 0) return w;
    }
    const sp = actor.system?.attributes?.speed;
    if (typeof sp === "number") return sp;
    if (sp && typeof sp.value === "number") return sp.value;
  } catch {
    /* ignore */
  }
  return null;
}

/** Max grid steps per turn ≈ walk speed ÷ feet per square (Manhattan). */
function computeMaxMovementSteps(actor) {
  const ft = getActorWalkSpeedFeet(actor);
  if (ft == null || ft <= 0) return null;
  const gridFt = getSceneGridDistanceFeet();
  if (!gridFt || gridFt <= 0) return null;
  return Math.max(0, Math.floor(ft / gridFt));
}

/**
 * Reduce (dx, dy) so |dx|+|dy| <= maxSteps, preserving direction approximately.
 * dx = grid columns (j), dy = rows (i), matching [[fas-move-rel]].
 */
function clampGridDelta(dx, dy, maxSteps) {
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  const m = ax + ay;
  if (maxSteps <= 0) return { dx: 0, dy: 0, used: 0 };
  if (m <= maxSteps) return { dx, dy, used: m };
  const sx = dx >= 0 ? 1 : -1;
  const sy = dy >= 0 ? 1 : -1;
  let rx = Math.floor((ax * maxSteps) / m);
  let ry = Math.floor((ay * maxSteps) / m);
  let used = rx + ry;
  let rem = maxSteps - used;
  while (rem > 0) {
    if (ax >= ay && rx < ax) {
      rx++;
      rem--;
    } else if (ry < ay) {
      ry++;
      rem--;
    } else if (rx < ax) {
      rx++;
      rem--;
    } else {
      break;
    }
  }
  return { dx: sx * rx, dy: sy * ry, used: rx + ry };
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

/**
 * Healing / temp HP spells & healing potions — allows `|target:` on **your** token and on **0 HP** non-hostiles
 * (allies; defeated **hostile** tokens are excluded — see `healingMayIncludeDefeatedToken`).
 * Weapons and offensive spells never use this.
 */
function dnd5eItemAllowsSelfTarget(item) {
  if (!item || game.system?.id !== "dnd5e") return false;
  const t = item.type;
  if (t === "weapon") return false;

  const partsHeal = (parts) => {
    if (!Array.isArray(parts)) return false;
    for (const p of parts) {
      const dt = String(p?.[1] ?? p?.damageType ?? p?.type ?? "").toLowerCase();
      if (dt === "healing" || dt === "temphp") return true;
    }
    return false;
  };

  if (t === "spell") {
    const sys = item.system ?? {};
    if (sys.actionType === "heal") return true;
    if (partsHeal(sys.damage?.parts)) return true;
    try {
      const acts = sys.activities;
      const arr = acts?.contents ? Array.from(acts.contents) : acts ? Array.from(acts) : [];
      for (const a of arr) {
        if (partsHeal(a?.damage?.parts) || partsHeal(a?.system?.damage?.parts)) return true;
      }
    } catch {
      /* ignore */
    }
    return false;
  }
  if (t === "consumable") {
    if (partsHeal(item.system?.damage?.parts)) return true;
    const sub = item.system?.type?.value ?? "";
    if (sub === "potion" && /\bhealing\b/i.test(item.name || "")) return true;
    return false;
  }
  return false;
}

function isAttackerToken(attackerActorId, token) {
  return token?.actor?.id && token.actor.id === attackerActorId;
}

/** dnd5e: 0 HP (or below) — unconscious / dead for automation; skip as target for attacks unless healing. */
function isActorDefeated(actor) {
  if (!actor || game.system?.id !== "dnd5e") return false;
  try {
    const hp = actor.system?.attributes?.hp;
    if (!hp) return false;
    const v = hp.value;
    let n;
    if (typeof v === "number" && Number.isFinite(v)) n = v;
    else if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) n = Number(v);
    else return false;
    return n <= 0;
  } catch {
    return false;
  }
}

/** Resolve a token on the active scene for targeting (name or actor id). */
function resolveTargetToken(attackerActorId, query, opts = {}) {
  const allowSelf = opts.allowSelf === true;
  const allowDefeated = opts.allowDefeated === true;
  if (!query?.trim() || !canvas?.ready) return null;
  const q = query.trim();
  const tokens = canvas.tokens?.placeables ?? [];
  if (looksLikeActorId(q)) {
    if (!allowSelf && q === attackerActorId) return null;
    const byActor = tokens.find((t) => t.actor?.id === q);
    if (byActor) {
      if (!allowSelf && isAttackerToken(attackerActorId, byActor)) return null;
      if (!allowDefeated && isActorDefeated(byActor.actor)) {
        console.warn("[Foundry Agent Studio] target is defeated (0 HP):", q);
        return null;
      }
      if (allowDefeated && !healingMayIncludeDefeatedToken(byActor)) return null;
      return byActor;
    }
  }
  const ql = q.toLowerCase();
  const candidates = tokens.filter((t) => {
    if (!allowSelf && t.actor?.id === attackerActorId) return false;
    if (!allowDefeated && isActorDefeated(t.actor)) return false;
    if (allowDefeated && !healingMayIncludeDefeatedToken(t)) return false;
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

function prepareTarget(attackerActorId, targetQuery, opts = {}) {
  const allowNearest = opts.allowNearest !== false;
  const allowSelf = opts.allowSelf === true;
  const allowDefeated = opts.allowDefeated === true;
  const resolveOpts = { allowSelf, allowDefeated };
  const raw = (targetQuery ?? "").trim();
  if (!raw) {
    if (allowNearest) return resolveNearestHostileToken(attackerActorId);
    return null;
  }
  const ql = raw.toLowerCase();
  if (
    ql === "nearest" ||
    ql === "nearest_hostile" ||
    ql === "closest" ||
    ql === "enemy" ||
    ql === "hostile"
  ) {
    return resolveNearestHostileToken(attackerActorId);
  }
  const token = resolveTargetToken(attackerActorId, raw, resolveOpts);
  if (!token) {
    console.warn("[Foundry Agent Studio] target not found on scene:", targetQuery);
    return null;
  }
  applyTargetSelection(token);
  logDistanceToTarget(attackerActorId, token);
  return token;
}

/**
 * When the midi-qol module is active, `MidiQOL.completeItemUse` runs the full item workflow
 * (attacks, saves, damage application per Midi settings). Optional — we fall back to vanilla dnd5e rolls.
 * @returns {object|null}
 */
function getMidiQolApi() {
  try {
    if (!game.modules.get("midi-qol")?.active) return null;
    const mq = globalThis.MidiQOL;
    if (mq && typeof mq.completeItemUse === "function") return mq;
  } catch {
    /* ignore */
  }
  return null;
}

/**
 * @param {Item} item
 * @param {Token|null} targetToken
 * @returns {Promise<boolean>} true if Midi finished the roll (caller skips vanilla path)
 */
async function tryMidiCompleteItemUse(item, targetToken) {
  const mq = getMidiQolApi();
  if (!mq) return false;
  try {
    const midiOptions = {
      fastForward: true,
      workflowOptions: {
        autoRollAttack: true,
        autoFastAttack: true,
        autoRollDamage: "onHit",
        autoFastDamage: true,
      },
    };
    const uuid = targetToken?.document?.uuid ?? targetToken?.uuid;
    if (uuid) {
      midiOptions.targetUuids = [uuid];
      midiOptions.ignoreUserTargets = true;
    }
    await mq.completeItemUse(item, { midiOptions }, { configure: false }, { create: true });
    return true;
  } catch (e) {
    console.warn("[Foundry Agent Studio] MidiQOL.completeItemUse failed, using vanilla rolls:", e);
    return false;
  }
}

/**
 * Collect roll instances from `Item#rollAttack` / ChatMessage (shape varies by Foundry + dnd5e version).
 * @param {unknown} attackResult
 * @returns {object[]}
 */
function collectAttackRollInstances(attackResult) {
  if (attackResult == null) return [];
  if (Array.isArray(attackResult)) return attackResult.filter(Boolean);
  const msg = attackResult.message ?? attackResult;
  if (msg?.rolls?.length) return Array.from(msg.rolls);
  if (attackResult.rolls?.length) return Array.from(attackResult.rolls);
  if (attackResult.roll) return [attackResult.roll];
  if (attackResult.terms) return [attackResult];
  return [];
}

/**
 * First attack D20Roll from `Item#rollAttack` — prefer a roll that looks like a d20 attack.
 * @param {unknown} attackResult
 * @returns {object|null}
 */
function extractFirstAttackRoll(attackResult) {
  const rolls = collectAttackRollInstances(attackResult);
  for (const r of rolls) {
    if (!r) continue;
    const d20 = r.d20 ?? r.terms?.[0];
    if (d20?.faces === 20) return r;
    if (r.terms?.some?.((t) => t?.faces === 20)) return r;
  }
  return rolls[0] ?? null;
}

/** Natural 20 on the d20 (handles advantage/disadvantage). */
function naturalTwentyOnAttackRoll(roll) {
  try {
    const d20 = roll?.d20 ?? roll?.terms?.[0];
    if (!d20?.results?.length || d20.faces !== 20) return false;
    const pool = d20.results.filter((r) => r.active !== false);
    const use = pool.length ? pool : d20.results;
    const mx = Math.max(...use.map((r) => Number(r.result)));
    return mx === 20;
  } catch {
    return false;
  }
}

/** Coerce numeric fields that may be strings (dnd5e data migration). */
function _numLike(v) {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

/**
 * Armor Class of the **target** of the attack (token’s actor, dnd5e).
 * Tries common `system.attributes.ac` shapes across system versions.
 */
function getActorAcFromToken(targetToken) {
  const actor = targetToken?.actor;
  if (!actor) return null;
  const g = (path) => {
    try {
      return _numLike(foundry.utils.getProperty(actor, path));
    } catch {
      return null;
    }
  };
  const acObj = actor.system?.attributes?.ac;
  const v =
    g("system.attributes.ac.value") ??
    g("system.attributes.ac.flat") ??
    _numLike(acObj?.value) ??
    _numLike(acObj?.flat) ??
    g("system.attributes.ac.calc");
  if (typeof v === "number" && Number.isFinite(v)) return v;
  return null;
}

/**
 * Reliable `.total` for Roll / D20Roll (some builds defer evaluation until accessed).
 * @param {object|null} roll
 * @returns {number|null}
 */
function numericRollTotal(roll) {
  if (!roll) return null;
  try {
    if (roll._evaluated === false && typeof roll.evaluate === "function") {
      roll.evaluate({ async: false });
    }
  } catch {
    /* still try .total */
  }
  return _numLike(roll.total);
}

/**
 * Hit = natural 20 (crit), or attack roll total meets/beats target AC (D&D 5e: total >= AC).
 * Uses AC from the target token’s actor and the numeric total from the attack roll.
 * @returns {{ hit: boolean, critical: boolean, reason: string }}
 */
function resolveWeaponAttackHit(attackResult, targetToken) {
  const rolls = collectAttackRollInstances(attackResult);
  const attackRoll = extractFirstAttackRoll(attackResult) ?? rolls[0] ?? null;
  if (!attackRoll) return { hit: false, critical: false, reason: "no_roll" };

  if (attackRoll.isFumble === true) {
    return { hit: false, critical: false, reason: "natural_1" };
  }

  if (naturalTwentyOnAttackRoll(attackRoll)) {
    return { hit: true, critical: true, reason: "natural_20" };
  }

  let total = numericRollTotal(attackRoll);
  if (total == null) {
    for (const r of rolls) {
      const t = numericRollTotal(r);
      if (t != null) {
        total = t;
        break;
      }
    }
  }

  const ac = targetToken ? getActorAcFromToken(targetToken) : null;

  if (typeof total !== "number" || typeof ac !== "number") {
    console.warn("[Foundry Agent Studio] Hit vs AC needs a numeric attack total and target AC.", {
      total,
      ac,
      targetName: targetToken?.name ?? targetToken?.actor?.name,
    });
    return { hit: false, critical: false, reason: "no_ac_or_total" };
  }

  if (total < ac) {
    return { hit: false, critical: false, reason: "below_ac" };
  }
  const crit = attackRoll.isCritical === true;
  return { hit: true, critical: !!crit, reason: crit ? "crit_on_hit" : "meets_ac" };
}

/**
 * Sum rolled damage from `Item#rollDamage` (return shape varies: Roll[], ChatMessage, Roll, or wrappers).
 */
function extractDamageTotalFromRollResult(result) {
  if (result == null) return null;
  const msg = result.message ?? result;
  if (msg?.rolls && Array.isArray(msg.rolls)) {
    let sum = 0;
    for (const r of msg.rolls) {
      if (r && typeof r.total === "number") sum += r.total;
    }
    if (sum > 0) return sum;
  }
  if (Array.isArray(result)) {
    let sum = 0;
    for (const r of result) {
      if (r && typeof r.total === "number") sum += r.total;
    }
    return sum > 0 ? sum : null;
  }
  if (typeof result.total === "number" && result.total > 0) return result.total;
  return null;
}

/**
 * Chat damage rolls do not reduce target HP by default. Apply to the targeted token's actor (dnd5e).
 */
async function applyRolledDamageToTargetActor(targetToken, damageRollResult) {
  const targetActor = targetToken?.actor;
  if (!targetActor || typeof targetActor.applyDamage !== "function") return;
  const total = extractDamageTotalFromRollResult(damageRollResult);
  if (total == null || !Number.isFinite(total) || total <= 0) {
    console.warn("[Foundry Agent Studio] No positive damage total to apply to target HP.");
    return;
  }
  try {
    await targetActor.applyDamage(total);
  } catch (e) {
    console.warn("[Foundry Agent Studio] applyDamage to target failed (permissions or system):", e);
  }
}

/**
 * @returns {Promise<boolean>} true if the sheet was updated (equip state changed)
 */
async function executeEquipItem(actorId, itemName, equippedFlag) {
  const actor = game.actors.get(actorId);
  if (!actor) {
    console.warn("[Foundry Agent Studio] fas-equip: no actor", actorId);
    return false;
  }
  if (game.system?.id !== "dnd5e") {
    console.warn("[Foundry Agent Studio] fas-equip requires dnd5e");
    return false;
  }
  const item = findItemByName(actor, itemName);
  if (!item) {
    console.warn("[Foundry Agent Studio] fas-equip: item not found:", itemName);
    return false;
  }
  const cur = !!item.system?.equipped;
  let next;
  if (equippedFlag === null || equippedFlag === undefined) {
    if (cur) {
      return false;
    }
    next = true;
  } else {
    next = !!equippedFlag;
  }
  if (next === cur) {
    return false;
  }
  try {
    await item.update({ "system.equipped": next });
    return true;
  } catch (e) {
    console.warn("[Foundry Agent Studio] fas-equip update failed:", itemName, e);
    return false;
  }
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
  const targetToken = prepareTarget(actorId, targetQuery ?? "", {
    allowSelf: false,
    allowDefeated: false,
  });
  if (targetToken && isAttackerToken(actorId, targetToken)) {
    console.warn(
      "[Foundry Agent Studio] fas-attack: cannot target yourself with weapons or attacks — pick an enemy or ally token."
    );
    return;
  }
  if (await tryMidiCompleteItemUse(item, targetToken)) {
    return;
  }
  const opts = { fastForward: true, chatMessage: true };
  if (targetToken) {
    opts.event = { target: targetToken };
  }
  try {
    if (typeof item.rollAttack === "function") {
      const attackResult = await item.rollAttack(opts);
      const hitInfo = resolveWeaponAttackHit(attackResult, targetToken);
      if (hitInfo.hit && typeof item.rollDamage === "function") {
        try {
          const atkRoll = extractFirstAttackRoll(attackResult);
          const dmgOpts = {
            fastForward: true,
            critical: !!hitInfo.critical,
            event: opts.event,
          };
          if (atkRoll) dmgOpts.attackRoll = atkRoll;
          const dmgResult = await item.rollDamage(dmgOpts);
          if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
        } catch (eDmg) {
          console.warn("[Foundry Agent Studio] rollDamage after weapon attack:", eDmg);
          try {
            const dmgResult = await item.rollDamage({
              fastForward: true,
              critical: !!hitInfo.critical,
              event: opts.event,
            });
            if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
          } catch (e2) {
            console.warn("[Foundry Agent Studio] rollDamage retry without attackRoll:", e2);
          }
        }
      }
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
  const healingLike = dnd5eItemAllowsSelfTarget(item);
  const targetToken = prepareTarget(actorId, targetQuery ?? "", {
    allowSelf: healingLike,
    allowDefeated: healingLike,
  });
  if (targetToken && isAttackerToken(actorId, targetToken) && !healingLike) {
    console.warn(
      "[Foundry Agent Studio] fas-spell: cannot target yourself with this spell — use healing/TEMP HP spells or potions for self."
    );
    return;
  }
  if (await tryMidiCompleteItemUse(item, targetToken)) {
    return;
  }
  const opts = { fastForward: true, chatMessage: true };
  if (targetToken) opts.event = { target: targetToken };
  try {
    if (typeof item.rollAttack === "function") {
      const attackResult = await item.rollAttack(opts);
      const hitInfo = resolveWeaponAttackHit(attackResult, targetToken);
      if (hitInfo.hit && typeof item.rollDamage === "function") {
        try {
          const atkRoll = extractFirstAttackRoll(attackResult);
          const dmgOpts = {
            fastForward: true,
            critical: !!hitInfo.critical,
            event: opts.event,
          };
          if (atkRoll) dmgOpts.attackRoll = atkRoll;
          const dmgResult = await item.rollDamage(dmgOpts);
          if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
        } catch (e2) {
          console.warn("[Foundry Agent Studio] spell rollDamage:", e2);
          try {
            const dmgResult = await item.rollDamage({
              fastForward: true,
              critical: !!hitInfo.critical,
              event: opts.event,
            });
            if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
          } catch (e3) {
            console.warn("[Foundry Agent Studio] spell rollDamage retry:", e3);
          }
        }
      }
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] spell rollAttack:", e);
  }
  try {
    if (typeof item.rollDamage === "function") {
      const dmgResult = await item.rollDamage({ fastForward: true, critical: false });
      if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
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
  const targetToken = prepareTarget(actorId, targetQuery, {
    allowNearest: false,
    allowSelf: false,
    allowDefeated: false,
  });
  if (targetToken && isAttackerToken(actorId, targetToken)) {
    console.warn("[Foundry Agent Studio] fas-damage: cannot apply weapon damage to yourself.");
    return;
  }
  const crit = !!critical;
  try {
    if (typeof item.rollDamage === "function") {
      const dmgResult = await item.rollDamage({ fastForward: true, critical: crit });
      if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
      return;
    }
  } catch (e) {
    console.warn("[Foundry Agent Studio] rollDamage:", e);
  }
  try {
    if (typeof item.rollDamage === "function") {
      const dmgResult = await item.rollDamage({ critical: crit });
      if (targetToken) await applyRolledDamageToTargetActor(targetToken, dmgResult);
    }
  } catch (e2) {
    console.warn("[Foundry Agent Studio] fas-damage retry:", e2);
  }
}

function logFasActionsToConsole(item, actions) {
  try {
    const rows = (actions ?? []).map((a) => {
      const t = a?.type ?? "?";
      if (t === "roll") return { type: t, formula: a.formula, flavor: a.flavor ?? "" };
      if (t === "move_rel") return { type: t, dx: a.dx, dy: a.dy };
      if (t === "move_abs") return { type: t, gx: a.gx, gy: a.gy };
      if (t === "equip_item") return { type: t, itemName: a.itemName, equipped: a.equipped };
      if (t === "attack_item" || t === "spell_item")
        return { type: t, itemName: a.itemName, targetQuery: a.targetQuery ?? "" };
      if (t === "damage_item")
        return { type: t, itemName: a.itemName, critical: a.critical, targetQuery: a.targetQuery ?? "" };
      return a;
    });
    console.info("[Foundry Agent Studio] FAS action batch", {
      actorId: item.actorId ?? null,
      messageId: item.id ?? null,
      count: rows.length,
      actions: rows,
    });
  } catch (e) {
    console.warn("[Foundry Agent Studio] FAS action log failed:", e);
  }
}

async function executeOutboxActions(item, chatSpeaker) {
  let actions = item.actions;
  if (!actions?.length && item.rolls?.length) {
    actions = legacyRollsToActions(item.rolls);
  }
  if (!actions?.length) return;

  logFasActionsToConsole(item, actions);

  const actorId = item.actorId;
  const actor = actorId ? game.actors.get(actorId) : null;
  let moveBudgetSteps =
    actor && canvas?.ready ? computeMaxMovementSteps(actor) : null;

  /** Defense in depth if the desktop app sends a bloated action list — match single-packet intent. */
  let equipDone = false;
  let movementDone = false;
  let strikeDone = false;

  for (const action of actions) {
    const t = action?.type;
    if (t === "roll") {
      await executeRoll(chatSpeaker, action);
    } else if (t === "move_rel" && actorId) {
      if (movementDone) {
        console.warn("[Foundry Agent Studio] Skipping extra move_rel in batch (one movement per packet).");
        continue;
      }
      movementDone = true;
      if (moveBudgetSteps != null) {
        const dx = action.dx ?? 0;
        const dy = action.dy ?? 0;
        const want = Math.abs(dx) + Math.abs(dy);
        const c = clampGridDelta(dx, dy, moveBudgetSteps);
        if (c.used < want) {
          console.warn(
            "[Foundry Agent Studio] move_rel (" +
              dx +
              "," +
              dy +
              ") clamped to (" +
              c.dx +
              "," +
              c.dy +
              ") — " +
              c.used +
              "/" +
              want +
              " steps (remaining budget was " +
              moveBudgetSteps +
              " steps)."
          );
        }
        moveBudgetSteps -= c.used;
        await executeMoveRel(actorId, c.dx, c.dy);
      } else {
        await executeMoveRel(actorId, action.dx ?? 0, action.dy ?? 0);
      }
    } else if (t === "move_abs" && actorId) {
      if (movementDone) {
        console.warn("[Foundry Agent Studio] Skipping extra move_abs in batch (one movement per packet).");
        continue;
      }
      movementDone = true;
      if (
        moveBudgetSteps != null &&
        canvas?.grid &&
        !canvas.grid.isGridless
      ) {
        const token = findTokenForActor(actorId);
        if (token && typeof canvas.grid.getOffset === "function") {
          const cur = canvas.grid.getOffset({ x: token.x, y: token.y });
          const gx = action.gx ?? 0;
          const gy = action.gy ?? 0;
          const dj = gx - cur.j;
          const di = gy - cur.i;
          const want = Math.abs(dj) + Math.abs(di);
          const c = clampGridDelta(dj, di, moveBudgetSteps);
          if (c.used < want) {
            console.warn(
              "[Foundry Agent Studio] move_abs toward (" +
                gx +
                "," +
                gy +
                ") clamped — " +
                c.used +
                "/" +
                want +
                " steps (budget " +
                moveBudgetSteps +
                ")."
            );
          }
          moveBudgetSteps -= c.used;
          const tgtGx = cur.j + c.dx;
          const tgtGy = cur.i + c.dy;
          await executeMoveAbs(actorId, tgtGx, tgtGy);
        } else {
          await executeMoveAbs(actorId, action.gx ?? 0, action.gy ?? 0);
        }
      } else {
        await executeMoveAbs(actorId, action.gx ?? 0, action.gy ?? 0);
      }
    } else if (t === "equip_item" && actorId) {
      if (equipDone) {
        console.warn(
          "[Foundry Agent Studio] Skipping extra equip_item in batch:",
          action.itemName ?? ""
        );
        continue;
      }
      const equipChanged = await executeEquipItem(actorId, action.itemName ?? "", action.equipped);
      if (equipChanged) {
        equipDone = true;
      }
    } else if (t === "attack_item" && actorId) {
      if (strikeDone) {
        console.warn(
          "[Foundry Agent Studio] Skipping extra attack_item in batch:",
          action.itemName ?? ""
        );
        continue;
      }
      strikeDone = true;
      await executeItemAttack(actorId, action.itemName ?? "", action.targetQuery);
    } else if (t === "spell_item" && actorId) {
      if (strikeDone) {
        console.warn(
          "[Foundry Agent Studio] Skipping extra spell_item in batch:",
          action.itemName ?? ""
        );
        continue;
      }
      strikeDone = true;
      await executeItemSpell(actorId, action.itemName ?? "", action.targetQuery);
    } else if (t === "damage_item" && actorId) {
      await executeItemDamage(actorId, action.itemName ?? "", action.critical, action.targetQuery);
    }
  }
}

async function postChatMessage(item) {
  const data = { user: item.userId, content: item.content };
  data.flags = { [MODULE_ID]: { fromOutbox: true } };
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
