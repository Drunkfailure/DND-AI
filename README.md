# Foundry Agent Studio

Production-minded **MVP**: a **Python** desktop-style app that runs **locally**, manages **AI player characters** (never the GM), talks to **Ollama** over `http://127.0.0.1:11434`, persists data in **SQLite**, and connects to **Foundry VTT** through a small **HTTP bridge** plus a **Foundry module** in `foundry-module/`.

The UI is **React + Vite** (`src/`), built to `web/dist/` and served by the same process as the API (default port **17890**).

## Architecture (summary)

| Layer | Role |
|--------|------|
| **React + Vite** (`src/`) | Agent CRUD UI, Ollama test chat (streaming NDJSON), memory tools, voice mock UI, bridge URL/secret display |
| **FastAPI** (`foundry_agent_studio/app.py`) | SQLite CRUD, Ollama chat/embeddings, optional `ollama serve` spawn, static files from `web/dist/` |
| **HTTP bridge** (same server, port from DB, default **17890**) | `POST /api/bridge/event` (`chat.received`, `world.connected`, `actor.sheet`, `combat.state`), `GET /api/bridge/outbox`, CORS for browser Foundry |
| **Foundry module** (`foundry-module/`) | Hooks `ready` + `createChatMessage` → app; combat hooks → **`combat.state`**; polls outbox → `ChatMessage.create`, then **Roll** / token moves as the linked **player user** |
| **Voice** (`voice_binaries.py`, `voice_paths.py`) | Mock TTS/STT; **Piper** (bundled + optional folder); **Whisper** (Windows x64 `whisper-cli` in `bin/windows/` + **`ggml-small.bin` you download** into `whisper_models/`, or a custom path in Settings) |

Data directory (Windows default): `%LOCALAPPDATA%\FoundryAgentStudio\foundry_agent_studio.db` — override via **Storage & data** in the UI (bootstrap file `paths.json` in that same folder points at a custom directory).

### Private vs party (test chat & Whisper voice)

- **Private:** Your message goes to the **selected** agent only (their short-term memory + reply).
- **Party:** The same utterance is appended to **every enabled** agent as a `Party: …` user line so everyone “heard” it; **only the selected agent** then generates an in-character reply (separate follow-up prompt). Use **Private** for secrets meant for one PC.
- **Party banter (AIs):** `POST /api/party/banter` runs a **round-robin** of short lines between **all enabled** player agents (2–12 total turns). **Disabled** while the Foundry **combat** snapshot shows an active encounter. Each reply is capped (500 chars), prompts discourage long scenes and automation directives, and lines are stored as `Banter: Name: …` for everyone else plus an **assistant** line for the speaker.

## SQLite schema (core)

- **`agents`** — id, name, description, system_prompt, model, temperature, memory_*, **world_wiki_url** / **world_wiki_notes** (optional campaign wiki link + pasted excerpt for world lore), **foundry_sheet_snapshot** (JSON from the Foundry module for linked actors), **memory_stm_guidance** / **memory_ltm_guidance**, **memory_stm_filter** / **memory_ltm_filter**, **memory_ltm_agent_curated**, embedding_model, voice_*, stt_*, voice_settings_json, foundry_*, **role** (default `player`), **knowledge_scope** (default `character`), is_enabled, timestamps.
- **`short_term_messages`** — per-agent chat lines (user/assistant) for short-term memory.
- **`long_term_memory`** — text + optional **embedding** blob (f32 LE) for semantic recall.
- **`app_config`** — key/value (`ollama_base`, `bridge_port`, `bridge_secret`, Piper/Whisper paths, optional **`foundry_combat_snapshot`** JSON from the module for turn awareness).
- **Data location** — by default the DB is under the OS app-data folder. **Storage & data** in the UI (or `paths.json` in that folder) can set a **custom folder** for `foundry_agent_studio.db`; **restart** the app after changing.

## Setup

### Prerequisites

- **Python 3.10+**
- **Node.js** (LTS) — only needed to build the web UI (`npm run build`).
- **Ollama** installed locally; pull a model, e.g. `ollama pull llama3.2` and an embedding model such as `nomic-embed-text`.

**Piper & whisper.cpp:** Piper uses **`piper-tts`**. Whisper uses **`whisper-cli`** plus a GGML/GGUF model file. On **Windows x64**, a prebuilt **`whisper-cli`** and DLLs ship under `foundry_agent_studio/bin/windows/` (see `bin/windows/README.txt`). On **Linux, macOS, or Windows ARM**, **delete that `bin/windows` folder** and install the binary for your platform from the [whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases) or [build from source](https://github.com/ggml-org/whisper.cpp). The default **small** Whisper weights are **not** in the git repo — download **`ggml-small.bin`** after clone (below).

| Component | What to get |
|-----------|-------------|
| **Piper** | Included as **`piper-tts`** (`pip install -r requirements.txt`). **Three English voices are bundled** under `foundry_agent_studio/voices/` (~60 MB each: `en_US-lessac-medium`, `en_US-ryan-low`, `en_GB-cori-medium`). If **Piper models folder** in the app is left empty, synthesis uses that bundled directory. See **Piper voices** below for adding more. [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl). Older CLI fallback: [rhasspy/piper](https://github.com/rhasspy/piper/releases). |
| **whisper.cpp** | **Weights:** place **`ggml-small.bin`** (~466 MiB) in `foundry_agent_studio/whisper_models/` ([download from Hugging Face](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin)) — leave **Whisper model file** blank in Settings to use it. **CLI:** **Windows x64** includes `whisper-cli` under `foundry_agent_studio/bin/windows/`. **Other platforms:** remove `bin/windows` and get `whisper-cli` from [releases](https://github.com/ggml-org/whisper.cpp/releases) or [build](https://github.com/ggml-org/whisper.cpp). Input: **16-bit PCM WAV** (upstream). |

### Piper voices (bundled default vs more)

**Bundled (no extra setup):** the repo includes three pretrained voices next to the Python package (`foundry_agent_studio/voices/`). Each voice is `<voice_id>.onnx` plus `<voice_id>.onnx.json`. New agents default to **`en_US-lessac-medium.onnx`**. Leave **Piper models folder** blank in the UI to use these files; or set a folder to **override** and load only voices you place there.

**More voices:** install the same `piper-tts` environment, then download any voice ID listed by:

```powershell
py -3 -m piper.download_voices
```

Download one or more into a folder you choose (or into `foundry_agent_studio/voices/` if you maintain a fork):

```powershell
py -3 -m piper.download_voices --download-dir "D:\path\to\piper-voices" en_US-lessac-high de_DE-thorsten-medium
```

Point **Piper models folder** at that directory in the app. The full catalog of IDs is also in `foundry_agent_studio/data/piper_voice_catalog.txt` and appears in the agent editor datalist.

**Repo size:** bundled `.onnx` files are large; clones may be slow unless you use Git LFS or strip voices in a fork.

### Whisper model (download `ggml-small.bin`)

**Weights are gitignored** (the file is ~466 MiB and exceeds GitHub’s per-file limit). After clone, download Whisper *small* into `foundry_agent_studio/whisper_models/`:

- **Direct link:** [ggerganov/whisper.cpp — `ggml-small.bin`](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin)

**PowerShell** (from the repo root):

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin" -OutFile "foundry_agent_studio\whisper_models\ggml-small.bin"
```

If **Whisper model file** in Settings is **empty**, the app uses `foundry_agent_studio/whisper_models/ggml-small.bin` when present. Set an absolute path in Settings to **override** (e.g. another GGUF/GGML from [the same Hugging Face repo](https://huggingface.co/ggerganov/whisper.cpp)).

**CLI:** **Windows x64:** `whisper-cli` + DLLs are included under **`foundry_agent_studio/bin/windows/`** (from [release v1.8.4](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.4) `whisper-bin-x64.zip`). Leave **whisper-cli executable** empty to use them. **Linux, macOS, Windows ARM, or any non–x64 Windows:** **delete `foundry_agent_studio/bin/windows/`** (those binaries will not run), then download the matching archive from [whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases) or [build from source](https://github.com/ggml-org/whisper.cpp), and set **whisper-cli executable** in the app (or use the sidecar filename next to `python.exe` as in `voice_binaries.py`).

**More models:** use other GGML/GGUF files from [ggerganov/whisper.cpp on Hugging Face](https://huggingface.co/ggerganov/whisper.cpp) or the whisper.cpp tree, then point the UI at that file.

### Python dependencies

```powershell
cd "d:\Projects\DND AI"
py -3 -m pip install -r requirements.txt
# or: py -3 -m pip install -e .
```

### Build the UI once

```powershell
npm install
npm run build
```

Output is written to `web/dist/`.

### Run the app

```powershell
py -3 -m foundry_agent_studio
```

Open **http://127.0.0.1:17890/** (or the port shown in the console if you changed `bridge_port` in the DB and restarted).

**Ollama models:** with Ollama running, the app loads **installed model names** from your daemon (`GET /api/tags`). Each agent’s **Chat model** and **Embedding model** fields use that list as suggestions (you can still type any name Ollama accepts). Use **Pull** on the **Ollama** card to download a model through the app (same as `ollama pull <name>`; first run can take a while).

**Development (hot reload UI + API):** in one terminal run `py -3 -m foundry_agent_studio`, in another run `npm run dev`. Vite proxies `/api` to `http://127.0.0.1:17890`.

### Foundry module

1. Copy `foundry-module` into your Foundry `Data/modules/foundry-agent-studio` folder (folder name must match module id).
2. Enable the module in your world.
3. In **Module Settings**, set **Bridge secret** to the value shown in the app (header `X-FAS-Secret`).
4. Link each agent in the app to a **Foundry user id** (and optionally **actor id** / **world id**).

### Bridge protocol

- **Module → app:** `POST /api/bridge/event` with header `X-FAS-Secret` and JSON `{ "type": "chat.received" | "world.connected" | "actor.sheet" | "combat.state", "payload": { ... } }`. **`combat.state`** may include **`battlefield`** (scene grid, token positions, walls) alongside **`combat`**.
- **App → module:** `GET /api/bridge/outbox` returns `{ "items": [ { "userId", "actorId?", "content", "actions?": [...], "rolls?": [...] } ] }`. **actions** may include `targetQuery` on **attack_item**, **spell_item**, **damage_item** (rolls duplicated under **rolls** for older clients). The module posts chat, then resolves targets and runs dnd5e rolls.

## Assumptions and limitations (MVP)

- **Character sheets:** the Foundry module sends **`actor.sheet`** events when PC actors update (debounced) and once shortly after world load. If an agent’s **Foundry actor id** (and optional **world id**) match, a JSON snapshot is stored in **`foundry_sheet_snapshot`** and injected into that agent’s system context (alongside wiki notes and memory). You can still paste extras in the system prompt or wiki notes.
- **Dice, attacks, spells, moves:** with a linked **actor id**, the prompt documents **`[[fas-roll:...]]`**, **`[[fas-attack:Item|target:...]]`**, **`[[fas-spell:Spell|target:...]]`**, **`[[fas-damage:Item|crit|target:...]]`**, **`[[fas-move-rel:...]]`**, **`[[fas-move-abs:...]]`**. The **sheet snapshot** (for **dnd5e**) includes per-item **range**, spell **level**/targeting hints, and an **aiTargetingNote**. The module resolves **target** to a token on the scene (name or **actorId**), sets Foundry targets, then rolls — **no auto-hit** vs AC; the GM still applies outcomes. **Gridless** scenes only support **move_rel**.
- **Combat / turns:** the module pushes **`combat.state`** (round, initiative **order**, HP summaries when the system exposes them, current **turn**). That text is injected for linked actors so PCs see party state and whether it is **their** turn. The server **drops** mechanical directives (rolls, moves, **attack_item**, **spell_item**, **damage_item**) from the outbox when the tracker says it is **not** that actor’s turn (only if their **actor id** appears in the synced order). Your own **sheet snapshot** still carries inventory and HP between turns; combat lines add encounter-wide context.
- **Battlefield (scene geometry):** the same payload can include **`battlefield`** — scene grid size, **combatant token** positions (`col`/`row` aligned with `[[fas-move-abs:col,row]]`), disposition (friendly/neutral/hostile), and **wall segments** (grid or pixel endpoints). Updates when combat changes and when tokens/walls move. **Hidden** GM tokens are omitted; **fog of war and true line-of-sight are not computed** — the GM remains the authority on what a PC can see.
- **World wiki URL:** the **server** fetches the page (public `http`/`https` only; SSRF-hardened), strips HTML to plain text, caches it in SQLite, and injects a snapshot into the model context (refreshed on a TTL or via **Fetch wiki now** in the UI). **Wiki / world notes** still lets you paste excerpts (locations, factions) to supplement or override thin pages.
- **First matching responder:** if several agents could reply to a line, the bridge picks the **first** enabled agent after filters (world + not self). For real play, keep **one** auto-responder per world or extend selection logic.
- **Foundry API variance:** different Foundry versions may differ slightly on `ChatMessage` / hooks; adjust `foundry-module/scripts/bridge.js` if your version uses different hook signatures.

## Stubbed vs working

| Area | Status |
|------|--------|
| Agent CRUD, SQLite, config | **Working** |
| Ollama chat + stream + embeddings | **Working** (requires Ollama running) |
| HTTP bridge + CORS | **Working** |
| Foundry module (chat in / out) | **Working** (with correct ids + secret) |
| Memory (short + long + semantic) | **Working** (embeddings depend on model) |
| Voice mock TTS/STT | **Working** (no audio playback) |
| Piper TTS | **Wired** via **`piper-tts`** + bundled voices (or your models folder); optional `piper` CLI fallback |
| Whisper STT | **Wired** when `whisper-cli` resolves (bundled Windows x64 or custom path) + downloaded **GGML/GGUF** model (see **Whisper model** above) |

## License

MIT OR Apache-2.0 at your discretion (project not yet licensed — add a `LICENSE` file if you redistribute).
