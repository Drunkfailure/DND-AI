import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiBase, apiJson, apiNdjsonStream } from "./api";
import { MockTTSProvider } from "./audio/mockProviders";
import { uint8ToBase64, WavRecorder } from "./audio/recordWav";
import { PLAYER_ONLY_PROMPT_SNIPPET } from "./constants";
import type { VoiceSettings } from "./audio/types";

type Agent = {
  id: string;
  name: string;
  description: string;
  systemPrompt: string;
  model: string;
  temperature: number;
  memoryShortTermLimit: number;
  memoryLongTermEnabled: boolean;
  embeddingModel: string;
  voiceProvider: string | null;
  voiceModel: string | null;
  sttProvider: string | null;
  voiceSettingsJson: string;
  foundryUserId: string;
  foundryActorId: string;
  foundryWorldId: string;
  role: string;
  knowledgeScope: string;
  isEnabled: boolean;
  memoryStmGuidance: string;
  memoryLtmGuidance: string;
  memoryStmFilter: string;
  memoryLtmFilter: string;
  /** When true, a follow-up call asks the same model for JSON facts to store (vs automatic interaction summary). */
  memoryLtmAgentCurated: boolean;
  /** Campaign wiki URL — the **server** fetches a public snapshot for the model (see worldWikiFetchedAt). */
  worldWikiUrl: string;
  /** Optional pasted wiki excerpt / locations (supplements the fetched page). */
  worldWikiNotes: string;
  /** When the wiki URL was last fetched server-side (RFC3339). */
  worldWikiFetchedAt?: string;
  /** Last JSON snapshot from Foundry (module pushes on sheet updates); injected into system context. */
  foundrySheetSnapshot: string;
};

type StorageInfo = {
  dataDirectory: string;
  effectiveDataDirectory: string;
  dbPath: string;
  bootstrapPath: string;
};

type BridgeStatus = {
  port: number;
  secret: string;
  ollamaBase: string;
};

type VoiceLocalPaths = {
  piperPath: string;
  piperModelsDir: string;
  /** Resolved models folder (user path, or bundled package voices if config empty). */
  piperModelsEffective?: string;
  whisperPath: string;
  whisperModelPath: string;
  /** Resolved ggml/gguf file (user path, or whisper_models/ggml-small.bin if present). */
  whisperModelEffective?: string;
  piperExeResolved: string | null;
  piperExeError: string | null;
  piperTtsAvailable?: boolean;
  whisperExeResolved: string | null;
  whisperExeError: string | null;
};

type OnnxFile = { name: string; path: string };

type PiperCatalogEntry = { id: string; onnxFile: string };

/** From GET /api/ollama/models — installed tags plus curated suggestions. */
type OllamaModelsBundle = {
  installed: string[];
  suggestedChat: string[];
  suggestedEmbed: string[];
};

const emptyOllamaModels = (): OllamaModelsBundle => ({
  installed: [],
  suggestedChat: [],
  suggestedEmbed: [],
});

/** Installed first, then suggested names not already listed (deduped). */
function mergeOllamaOptions(installed: string[], suggested: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const m of installed) {
    const t = m.trim();
    if (t && !seen.has(t)) {
      seen.add(t);
      out.push(t);
    }
  }
  for (const m of suggested) {
    const t = m.trim();
    if (t && !seen.has(t)) {
      seen.add(t);
      out.push(t);
    }
  }
  return out;
}

/** Voices actually present: one datalist entry per `.onnx` in the effective Piper models folder. */
function voiceOptionsFromOnnxFiles(onnxFiles: OnnxFile[]): PiperCatalogEntry[] {
  const out: PiperCatalogEntry[] = [];
  for (const f of onnxFiles) {
    const name = (f.name || "").trim();
    if (!name.toLowerCase().endsWith(".onnx")) continue;
    const id = name.replace(/\.onnx$/i, "");
    out.push({ id, onnxFile: name });
  }
  return out.sort((a, b) => a.onnxFile.localeCompare(b.onnxFile));
}

const DEFAULT_AGENT_PIPER_ONNX = "en_US-lessac-medium.onnx";

function agentPiperOnnxFile(agent: Agent | null, testFallback: string): string {
  const m = agent?.voiceModel?.trim();
  return m || testFallback;
}

const defaultVoiceSettings = (): VoiceSettings => ({
  voiceId: "mock-1",
  rate: 1,
  pitch: 1,
});

export default function App() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [bridge, setBridge] = useState<BridgeStatus | null>(null);
  const [ollamaUrl, setOllamaUrl] = useState("http://127.0.0.1:11434");
  const [bridgePort, setBridgePort] = useState("17890");
  const [ollamaOk, setOllamaOk] = useState<boolean | null>(null);
  const [ollamaModels, setOllamaModels] = useState<OllamaModelsBundle>(emptyOllamaModels());
  const [pullModelName, setPullModelName] = useState("");
  const [pullBusy, setPullBusy] = useState(false);
  const [storageInfo, setStorageInfo] = useState<StorageInfo | null>(null);
  const [dataDirInput, setDataDirInput] = useState("");
  const [chatInput, setChatInput] = useState("");
  const [chatOut, setChatOut] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [banterTopic, setBanterTopic] = useState("");
  const [banterMaxTurns, setBanterMaxTurns] = useState(4);
  const [banterBusy, setBanterBusy] = useState(false);
  const [wikiRefreshBusy, setWikiRefreshBusy] = useState(false);
  const [memQuery, setMemQuery] = useState("");
  const [memHits, setMemHits] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [piperPath, setPiperPath] = useState("");
  const [piperModelsDir, setPiperModelsDir] = useState("");
  const [whisperPath, setWhisperPath] = useState("");
  const [whisperModelPath, setWhisperModelPath] = useState("");
  const [voiceStatus, setVoiceStatus] = useState<VoiceLocalPaths | null>(null);
  const [onnxList, setOnnxList] = useState<OnnxFile[]>([]);
  const [piperTestModel, setPiperTestModel] = useState("en_US-lessac-medium.onnx");
  /** Private = only this agent's memory; Party = line stored for every enabled agent, then this agent speaks. */
  const [chatScope, setChatScope] = useState<"private" | "party">("private");
  const [speakReplies, setSpeakReplies] = useState(true);
  const [voiceConvBusy, setVoiceConvBusy] = useState(false);
  const [voiceRecording, setVoiceRecording] = useState(false);
  const wavRef = useRef<WavRecorder | null>(null);

  const mockTts = useMemo(() => new MockTTSProvider(), []);

  const selected = agents.find((a) => a.id === selectedId) ?? null;
  const whisperReady = Boolean(
    voiceStatus?.whisperExeResolved &&
      (whisperModelPath.trim() || voiceStatus?.whisperModelEffective?.trim()),
  );
  const piperModelsReady = Boolean(
    voiceStatus?.piperModelsEffective?.trim() || piperModelsDir.trim(),
  );

  const chatModelOptions = useMemo(
    () => mergeOllamaOptions(ollamaModels.installed, ollamaModels.suggestedChat),
    [ollamaModels],
  );
  const embedModelOptions = useMemo(
    () => mergeOllamaOptions(ollamaModels.installed, ollamaModels.suggestedEmbed),
    [ollamaModels],
  );
  /** Pull UI: chat + embedding suggestions plus installed (deduped). */
  const pullModelOptions = useMemo(
    () =>
      mergeOllamaOptions(
        mergeOllamaOptions(ollamaModels.installed, ollamaModels.suggestedChat),
        ollamaModels.suggestedEmbed,
      ),
    [ollamaModels],
  );

  const agentPiperVoiceOptions = useMemo(() => voiceOptionsFromOnnxFiles(onnxList), [onnxList]);

  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const list = await apiJson<Agent[]>("/api/agents");
      setAgents(list);
      const b = await apiJson<BridgeStatus>("/api/bridge/status");
      setBridge(b);
      setOllamaUrl(b.ollamaBase);
      setBridgePort(String(b.port));
      const vs = await apiJson<VoiceLocalPaths>("/api/voice/local/paths");
      setVoiceStatus(vs);
      setPiperPath(vs.piperPath);
      setPiperModelsDir(vs.piperModelsDir);
      setWhisperPath(vs.whisperPath);
      setWhisperModelPath(vs.whisperModelPath);
      try {
        const ox = await apiJson<OnnxFile[]>("/api/voice/piper/onnx");
        setOnnxList(ox);
      } catch {
        setOnnxList([]);
      }
      try {
        const ok = await apiJson<boolean>("/api/ollama/health");
        setOllamaOk(ok);
      } catch {
        setOllamaOk(false);
      }
      try {
        const mm = await apiJson<OllamaModelsBundle>("/api/ollama/models");
        setOllamaModels(mm);
      } catch {
        setOllamaModels(emptyOllamaModels());
      }
      try {
        const si = await apiJson<StorageInfo>("/api/settings/storage");
        setStorageInfo(si);
        setDataDirInput(si.dataDirectory || "");
      } catch {
        setStorageInfo(null);
      }
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const saveOllamaUrl = async () => {
    setErr(null);
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "ollama_base", value: ollamaUrl }),
    });
    await refresh();
  };

  const saveBridgePort = async () => {
    setErr(null);
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "bridge_port", value: bridgePort }),
    });
    await refresh();
  };

  const saveVoicePaths = async () => {
    setErr(null);
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "piper_path", value: piperPath }),
    });
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "piper_models_dir", value: piperModelsDir }),
    });
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "whisper_path", value: whisperPath }),
    });
    await apiJson("/api/config", {
      method: "POST",
      body: JSON.stringify({ key: "whisper_model_path", value: whisperModelPath }),
    });
    await refresh();
  };

  const testPiperLocal = async () => {
    setErr(null);
    try {
      const j = await apiJson<{ bytesBase64: string }>("/api/voice/piper/synthesize", {
        method: "POST",
        body: JSON.stringify({ text: "Hello from Piper.", modelFile: piperTestModel }),
      });
      const bin = atob(j.bytesBase64);
      setErr(`Piper OK — generated ${bin.length} bytes of WAV (play in a future player).`);
    } catch (e) {
      setErr(String(e));
    }
  };

  const testWhisperLocal = async () => {
    setErr(null);
    setErr(
      "Whisper test needs WAV bytes (RIFF). Record WAV in the UI later, or use mock STT for now.",
    );
  };

  const checkOllama = async () => {
    setErr(null);
    try {
      const v = await apiJson<boolean>("/api/ollama/health");
      setOllamaOk(v);
    } catch {
      setOllamaOk(false);
    }
    try {
      const mm = await apiJson<OllamaModelsBundle>("/api/ollama/models");
      setOllamaModels(mm);
    } catch {
      setOllamaModels(emptyOllamaModels());
    }
  };

  const launchOllama = async () => {
    setErr(null);
    const j = await apiJson<{ message: string }>("/api/ollama/launch", { method: "POST" });
    setErr(j.message);
  };

  const saveStorage = async () => {
    setErr(null);
    try {
      const j = await apiJson<{ ok: boolean; restartRequired?: boolean; message: string }>(
        "/api/settings/storage",
        {
          method: "POST",
          body: JSON.stringify({ dataDirectory: dataDirInput.trim() }),
        },
      );
      setErr(j.message);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const clearStorageLocation = async () => {
    setErr(null);
    try {
      const j = await apiJson<{ ok: boolean; message: string }>("/api/settings/storage", {
        method: "POST",
        body: JSON.stringify({ dataDirectory: "" }),
      });
      setDataDirInput("");
      setErr(j.message);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  };

  const pullOllamaModel = async () => {
    const name = pullModelName.trim();
    if (!name) return;
    setErr(null);
    setPullBusy(true);
    try {
      await apiJson<Record<string, unknown>>("/api/ollama/pull", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setPullModelName("");
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setPullBusy(false);
    }
  };

  const createAgent = async () => {
    setErr(null);
    const a = await apiJson<Agent>("/api/agents", {
      method: "POST",
      body: JSON.stringify({
        name: "New PC",
        description: "",
        systemPrompt: "You are brave, cautious, and speak in short sentences.",
        model: "llama3.2",
        temperature: 0.7,
        voiceProvider: "piper",
        voiceModel: DEFAULT_AGENT_PIPER_ONNX,
      }),
    });
    setAgents((prev) => [...prev, a]);
    setSelectedId(a.id);
  };

  const updateField = async (patch: Partial<Agent> & { id: string }) => {
    setErr(null);
    const updated = await apiJson<Agent>("/api/agents", {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    setAgents((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
  };

  const deleteAgent = async (id: string) => {
    setErr(null);
    await apiJson(`/api/agents/${encodeURIComponent(id)}`, { method: "DELETE" });
    setAgents((prev) => prev.filter((a) => a.id !== id));
    if (selectedId === id) setSelectedId(null);
  };

  const runChat = async () => {
    if (!selected) return;
    const msg = chatInput.trim();
    if (!msg) return;
    setErr(null);
    setStreaming(true);
    setChatOut("");
    try {
      if (chatScope === "party") {
        await apiJson("/api/party/broadcast", {
          method: "POST",
          body: JSON.stringify({ text: msg }),
        });
        await apiNdjsonStream(
          "/api/ollama/chat/stream",
          { agentId: selected.id, userMessage: "", partyFollowup: true },
          (chunk) => setChatOut((c) => c + chunk),
        );
      } else {
        await apiNdjsonStream(
          "/api/ollama/chat/stream",
          { agentId: selected.id, userMessage: msg },
          (chunk) => setChatOut((c) => c + chunk),
        );
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setStreaming(false);
    }
  };

  const refreshWikiNow = async () => {
    if (!selected?.id || !selected.worldWikiUrl?.trim()) return;
    setErr(null);
    setWikiRefreshBusy(true);
    try {
      const updated = await apiJson<Agent>(
        `/api/agents/${encodeURIComponent(selected.id)}/wiki/refresh`,
        { method: "POST" },
      );
      setAgents((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
    } catch (e) {
      setErr(String(e));
    } finally {
      setWikiRefreshBusy(false);
    }
  };

  const runBanter = async () => {
    setErr(null);
    setBanterBusy(true);
    setChatOut("");
    try {
      const j = await apiJson<{ lines: { name: string; text: string }[]; turns: number }>("/api/party/banter", {
        method: "POST",
        body: JSON.stringify({
          maxTurns: Math.min(12, Math.max(2, banterMaxTurns)),
          topic: banterTopic.trim(),
        }),
      });
      setChatOut(j.lines.map((l) => `${l.name}: ${l.text}`).join("\n\n"));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBanterBusy(false);
    }
  };

  const playAgentReply = async (text: string) => {
    if (!speakReplies || !text.trim()) return;
    try {
      if ((voiceStatus?.piperExeResolved || voiceStatus?.piperTtsAvailable) && piperModelsReady) {
        const j = await apiJson<{ bytesBase64: string }>("/api/voice/piper/synthesize", {
          method: "POST",
          body: JSON.stringify({
            text: text.slice(0, 8000),
            modelFile: agentPiperOnnxFile(selected, piperTestModel),
          }),
        });
        const bin = atob(j.bytesBase64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const url = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
        const audio = new Audio(url);
        await new Promise<void>((resolve, reject) => {
          audio.onended = () => {
            URL.revokeObjectURL(url);
            resolve();
          };
          audio.onerror = () => {
            URL.revokeObjectURL(url);
            reject(new Error("playback failed"));
          };
          void audio.play();
        });
      } else {
        await new Promise<void>((resolve) => {
          const u = new SpeechSynthesisUtterance(text);
          u.onend = () => resolve();
          u.onerror = () => resolve();
          window.speechSynthesis.speak(u);
        });
      }
    } catch {
      const u = new SpeechSynthesisUtterance(text);
      window.speechSynthesis.speak(u);
    }
  };

  const runVoiceChat = async (userText: string) => {
    if (!selected || !userText.trim()) return;
    const line = userText.trim();
    setErr(null);
    setVoiceConvBusy(true);
    setChatInput(chatScope === "party" ? `[Party] ${line}` : line);
    setChatOut("");
    setStreaming(true);
    let full = "";
    try {
      if (chatScope === "party") {
        await apiJson("/api/party/broadcast", {
          method: "POST",
          body: JSON.stringify({ text: line }),
        });
        await apiNdjsonStream(
          "/api/ollama/chat/stream",
          { agentId: selected.id, userMessage: "", partyFollowup: true },
          (chunk) => {
            full += chunk;
            setChatOut((c) => c + chunk);
          },
        );
      } else {
        await apiNdjsonStream(
          "/api/ollama/chat/stream",
          { agentId: selected.id, userMessage: line },
          (chunk) => {
            full += chunk;
            setChatOut((c) => c + chunk);
          },
        );
      }
      if (speakReplies && full.trim()) await playAgentReply(full.trim());
    } catch (e) {
      setErr(String(e));
    } finally {
      setStreaming(false);
      setVoiceConvBusy(false);
    }
  };

  const onWhisperDown = async () => {
    if (!selected || voiceConvBusy || streaming) return;
    if (!whisperReady) {
      setErr("Configure whisper-cli path and model above, then Save & Refresh.");
      return;
    }
    const rec = new WavRecorder();
    wavRef.current = rec;
    try {
      await rec.start();
      setVoiceRecording(true);
    } catch (e) {
      wavRef.current = null;
      setErr(String(e));
    }
  };

  const onWhisperUp = async () => {
    if (!wavRef.current) return;
    setVoiceRecording(false);
    const rec = wavRef.current;
    wavRef.current = null;
    try {
      const wav = await rec.stop();
      if (wav.length < 500) {
        setErr("Recording too short — hold the button while speaking.");
        return;
      }
      const j = await apiJson<{ text: string }>("/api/voice/whisper/transcribe", {
        method: "POST",
        body: JSON.stringify({ audioBase64: uint8ToBase64(wav) }),
      });
      const text = j.text.trim();
      if (!text) {
        setErr("Whisper returned empty text — try again or check the mic.");
        return;
      }
      await runVoiceChat(text);
    } catch (e) {
      setErr(String(e));
      setVoiceConvBusy(false);
    }
  };

  const memorySearch = async () => {
    if (!selected) return;
    setErr(null);
    const hits = await apiJson<string[]>("/api/memory/search", {
      method: "POST",
      body: JSON.stringify({ agentId: selected.id, query: memQuery, topK: 8 }),
    });
    setMemHits(hits);
  };

  const memoryEmbed = async () => {
    if (!selected || !memQuery.trim()) return;
    setErr(null);
    await apiJson("/api/memory/embed", {
      method: "POST",
      body: JSON.stringify({ agentId: selected.id, text: memQuery.trim() }),
    });
  };

  const testVoice = async () => {
    if (!selected) return;
    setErr(null);
    const settings = defaultVoiceSettings();
    try {
      await mockTts.speak(`Hello from ${selected.name}.`, settings);
    } catch (e) {
      setErr(String(e));
    }
  };

  const bridgeUrl = bridge ? `${window.location.protocol}//127.0.0.1:${bridge.port}` : "";

  return (
    <div className="app">
      <header className="header">
        <h1>Foundry Agent Studio</h1>
        <p className="subtitle">
          Local AI player characters for Foundry VTT. You are always the GM; agents never run the world.
        </p>
        <p className="muted small">
          API: <code>{apiBase() || "(same origin)"}</code>
        </p>
      </header>

      {err && <div className="banner banner-warn">{err}</div>}

      <div className="grid">
        <section className="card">
          <h2>Ollama</h2>
          <label>
            Base URL
            <input
              value={ollamaUrl}
              onChange={(e) => setOllamaUrl(e.target.value)}
              className="input"
            />
          </label>
          <div className="row">
            <button type="button" onClick={() => void saveOllamaUrl()}>
              Save
            </button>
            <button type="button" onClick={() => void checkOllama()}>
              Check &amp; list models
            </button>
            <button type="button" onClick={() => void launchOllama()}>
              Try launch <code>ollama serve</code>
            </button>
          </div>
          <p className="muted">
            Status: {ollamaOk === null ? "—" : ollamaOk ? "reachable" : "unreachable"}
          </p>
          {ollamaModels.installed.length > 0 && (
            <p className="muted small">Installed models: {ollamaModels.installed.join(", ")}</p>
          )}
          <label>
            Pull a model into Ollama (<code>ollama pull …</code>)
            <div className="row" style={{ marginTop: "0.35rem" }}>
              <input
                className="input"
                list="ollama-pull-models"
                autoComplete="off"
                value={pullModelName}
                onChange={(e) => setPullModelName(e.target.value)}
                placeholder="Choose or type a model name"
                disabled={pullBusy}
              />
              <button type="button" disabled={pullBusy || !pullModelName.trim()} onClick={() => void pullOllamaModel()}>
                {pullBusy ? "Pulling…" : "Pull"}
              </button>
            </div>
            <datalist id="ollama-pull-models">
              {pullModelOptions.map((m) => (
                <option key={`pull-${m}`} value={m} />
              ))}
            </datalist>
            <p className="muted small" style={{ marginTop: "0.35rem" }}>
              Dropdown lists installed models and common names to pull; you can still type any Ollama library name.
            </p>
          </label>
          <p className="muted small">
            First download can take several minutes. Uses your configured Ollama base URL.
          </p>
        </section>

        <section className="card">
          <h2>Foundry bridge</h2>
          <p className="small">
            HTTP server on localhost. The Foundry module posts <code>chat.received</code> and polls{" "}
            <code>outbox</code>. Copy the secret into the module settings.
          </p>
          <label>
            Port
            <input
              value={bridgePort}
              onChange={(e) => setBridgePort(e.target.value)}
              className="input"
            />
          </label>
          <div className="row">
            <button type="button" onClick={() => void saveBridgePort()}>
              Save port (restart app to rebind)
            </button>
          </div>
          {bridge && (
            <dl className="kv">
              <dt>Bridge URL</dt>
              <dd>
                <code>{bridgeUrl}</code>
              </dd>
              <dt>Secret header</dt>
              <dd>
                <code className="mono wrap">X-FAS-Secret: {bridge.secret}</code>
              </dd>
            </dl>
          )}
        </section>

        <section className="card">
          <h2>Storage &amp; data</h2>
          <p className="small muted">
            Choose a folder for the SQLite database and all app data. A small bootstrap file in your OS app-data
            folder points at this path. <strong>Restart the app</strong> after changing.
          </p>
          {storageInfo && (
            <dl className="kv">
              <dt>Effective data folder</dt>
              <dd className="mono wrap">{storageInfo.effectiveDataDirectory}</dd>
              <dt>Database file</dt>
              <dd className="mono wrap">{storageInfo.dbPath}</dd>
              <dt>Bootstrap file</dt>
              <dd className="mono wrap">{storageInfo.bootstrapPath}</dd>
            </dl>
          )}
          <label>
            Custom data folder (empty = default OS location)
            <input
              className="input"
              value={dataDirInput}
              onChange={(e) => setDataDirInput(e.target.value)}
              placeholder="e.g. D:\MyGames\FoundryAgentStudio"
            />
          </label>
          <div className="row">
            <button type="button" onClick={() => void saveStorage()}>
              Save data location
            </button>
            <button
              type="button"
              onClick={() => void clearStorageLocation()}
            >
              Use default location
            </button>
          </div>
        </section>
      </div>

      <section className="card">
        <h2>Local voice — Piper &amp; whisper.cpp</h2>
        <p className="small muted">
          <strong>Piper:</strong>{" "}
          <a href="https://github.com/OHF-Voice/piper1-gpl" target="_blank" rel="noopener noreferrer">
            OHF-Voice/piper1-gpl
          </a>{" "}
          via <code>pip install -r requirements.txt</code>. <strong>Whisper:</strong>{" "}
          <a href="https://github.com/ggml-org/whisper.cpp" target="_blank" rel="noopener noreferrer">
            ggml-org/whisper.cpp
          </a>
          — on <strong>Windows x64</strong>, <code>whisper-cli</code> ships under the package (leave the path empty).
          On <strong>Linux, macOS, or other Windows arch</strong>, remove <code>foundry_agent_studio/bin/windows</code> and
          install <code>whisper-cli</code> from the repo’s{" "}
          <a href="https://github.com/ggml-org/whisper.cpp/releases" target="_blank" rel="noopener noreferrer">
            releases
          </a>{" "}
          (see README). Download <code>ggml-small.bin</code> into <code>whisper_models/</code> (not in git). Optional: set
          paths below or place sidecars next to Python.
        </p>
        <label>
          Piper executable (optional if sidecar beside app)
          <input
            className="input"
            value={piperPath}
            onChange={(e) => setPiperPath(e.target.value)}
            placeholder="C:\tools\piper\piper.exe"
          />
        </label>
        <label>
          Piper models folder (optional if bundled voices ship with the package)
          <input
            className="input"
            value={piperModelsDir}
            onChange={(e) => setPiperModelsDir(e.target.value)}
            placeholder="Leave empty to use bundled voices"
          />
        </label>
        {voiceStatus?.piperModelsEffective &&
          !piperModelsDir.trim() &&
          voiceStatus.piperModelsEffective.trim() && (
            <p className="small muted">
              Using bundled Piper models: <code className="mono">{voiceStatus.piperModelsEffective}</code>
            </p>
          )}
        <label>
          whisper-cli executable (optional on Windows x64 — bundled; other OS: set path or see README)
          <input
            className="input"
            value={whisperPath}
            onChange={(e) => setWhisperPath(e.target.value)}
            placeholder="Leave empty on Windows x64 to use bundled whisper-cli"
          />
        </label>
        <label>
          Whisper model file (ggml / gguf — leave empty if <code>whisper_models/ggml-small.bin</code> exists)
          <input
            className="input"
            value={whisperModelPath}
            onChange={(e) => setWhisperModelPath(e.target.value)}
            placeholder="Leave empty for whisper_models/ggml-small.bin (download per README)"
          />
        </label>
        {voiceStatus?.whisperModelEffective &&
          !whisperModelPath.trim() &&
          voiceStatus.whisperModelEffective.trim() && (
            <p className="small muted">
              Using default Whisper model: <code className="mono">{voiceStatus.whisperModelEffective}</code>
            </p>
          )}
        <div className="row">
          <button type="button" onClick={() => void saveVoicePaths()}>
            Save voice paths
          </button>
          <button type="button" onClick={() => void refresh()}>
            Refresh status
          </button>
        </div>
        {voiceStatus && (
          <dl className="kv">
            <dt>Piper models (effective)</dt>
            <dd className="mono wrap">
              {voiceStatus.piperModelsEffective?.trim()
                ? voiceStatus.piperModelsEffective
                : "— (set folder or install bundled voices)"}
            </dd>
            <dt>Piper resolved</dt>
            <dd className="mono wrap">
              {voiceStatus.piperExeResolved ?? voiceStatus.piperExeError ?? "—"}
            </dd>
            <dt>Whisper model (effective)</dt>
            <dd className="mono wrap">
              {voiceStatus.whisperModelEffective?.trim()
                ? voiceStatus.whisperModelEffective
                : "— (set path or download ggml-small.bin into whisper_models/)"}
            </dd>
            <dt>whisper-cli resolved</dt>
            <dd className="mono wrap">
              {voiceStatus.whisperExeResolved ?? voiceStatus.whisperExeError ?? "—"}
            </dd>
          </dl>
        )}
        <label>
          Test Piper — model filename
          <input
            className="input"
            list="fas-piper-voices-merged"
            autoComplete="off"
            value={piperTestModel}
            onChange={(e) => setPiperTestModel(e.target.value)}
            placeholder="en_US-lessac-medium.onnx"
          />
        </label>
        <datalist id="fas-piper-voices-merged">
          {agentPiperVoiceOptions.map((o) => (
            <option key={o.onnxFile} value={o.onnxFile} label={o.id} />
          ))}
        </datalist>
        <div className="row">
          <button type="button" onClick={() => void testPiperLocal()}>
            Test Piper (synthesize)
          </button>
          <button type="button" onClick={() => void testWhisperLocal()}>
            Whisper help
          </button>
        </div>
        {onnxList.length > 0 && (
          <p className="small muted">
            Models in folder: {onnxList.map((o) => o.name).join(", ")}
          </p>
        )}
      </section>

      <section className="card">
        <h2>Agents (player characters)</h2>
        <div className="row">
          <button type="button" onClick={() => void createAgent()}>
            New agent
          </button>
        </div>
        <div className="agent-layout">
          <ul className="agent-list">
            {agents.map((a) => (
              <li key={a.id}>
                <button
                  type="button"
                  className={a.id === selectedId ? "active" : ""}
                  onClick={() => setSelectedId(a.id)}
                >
                  {a.name}
                </button>
              </li>
            ))}
          </ul>
          {selected && (
            <div className="agent-form">
              <label>
                Name
                <input
                  className="input"
                  value={selected.name}
                  onChange={(e) => void updateField({ id: selected.id, name: e.target.value })}
                />
              </label>
              <label>
                Enabled
                <input
                  type="checkbox"
                  checked={selected.isEnabled}
                  onChange={(e) => void updateField({ id: selected.id, isEnabled: e.target.checked })}
                />
              </label>
              <label>
                Chat model (Ollama)
                <input
                  className="input"
                  list={`ollama-chat-${selected.id}`}
                  autoComplete="off"
                  value={selected.model}
                  onChange={(e) => void updateField({ id: selected.id, model: e.target.value })}
                  placeholder="llama3.2"
                />
                <datalist id={`ollama-chat-${selected.id}`}>
                  {chatModelOptions.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
                <p className="small muted" style={{ marginTop: "0.35rem" }}>
                  Suggestions include common library names; your installed models are listed first. Pull a model on the
                  Ollama card, then refresh. You can still type any name <code>ollama pull</code> supports.
                </p>
              </label>
              <label>
                Temperature
                <input
                  type="number"
                  step="0.05"
                  min={0}
                  max={2}
                  className="input"
                  value={selected.temperature}
                  onChange={(e) =>
                    void updateField({ id: selected.id, temperature: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                Foundry user id (player)
                <input
                  className="input"
                  value={selected.foundryUserId}
                  onChange={(e) =>
                    void updateField({ id: selected.id, foundryUserId: e.target.value })
                  }
                  placeholder="e.g. User id from Foundry"
                />
              </label>
              <label>
                Foundry actor id (optional)
                <input
                  className="input"
                  value={selected.foundryActorId}
                  onChange={(e) =>
                    void updateField({ id: selected.id, foundryActorId: e.target.value })
                  }
                />
              </label>
              <label>
                Foundry world id (optional filter)
                <input
                  className="input"
                  value={selected.foundryWorldId}
                  onChange={(e) =>
                    void updateField({ id: selected.id, foundryWorldId: e.target.value })
                  }
                />
              </label>
              <label>
                Cached Foundry sheet (read-only — synced by the module when the actor updates)
                <textarea
                  className="textarea"
                  rows={5}
                  readOnly
                  value={selected.foundrySheetSnapshot || ""}
                  placeholder="Open the world in Foundry with the module enabled; sheet JSON appears here after updates."
                />
                <button
                  type="button"
                  className="button secondary"
                  style={{ marginTop: "0.35rem" }}
                  onClick={() =>
                    void updateField({ id: selected.id, foundrySheetSnapshot: "" })
                  }
                >
                  Clear cached sheet
                </button>
              </label>
              <label>
                World wiki URL (optional — server fetches a text snapshot for the model)
                <input
                  className="input"
                  type="url"
                  value={selected.worldWikiUrl}
                  onChange={(e) => void updateField({ id: selected.id, worldWikiUrl: e.target.value })}
                  placeholder="https://wiki.example.com/your-campaign"
                />
                {selected.worldWikiFetchedAt ? (
                  <p className="small muted" style={{ marginTop: "0.35rem" }}>
                    Last fetched: {selected.worldWikiFetchedAt}
                  </p>
                ) : null}
                <button
                  type="button"
                  className="button secondary"
                  style={{ marginTop: "0.35rem" }}
                  disabled={wikiRefreshBusy || !selected.worldWikiUrl?.trim()}
                  onClick={() => void refreshWikiNow()}
                >
                  {wikiRefreshBusy ? "Fetching…" : "Fetch wiki now"}
                </button>
              </label>
              <label>
                Wiki / world notes (optional — paste locations &amp; lore to supplement the fetched page)
                <textarea
                  className="textarea"
                  rows={4}
                  value={selected.worldWikiNotes}
                  onChange={(e) => void updateField({ id: selected.id, worldWikiNotes: e.target.value })}
                  placeholder="Paste key wiki sections so this PC can stay consistent with places and factions."
                />
              </label>
              <label>
                Personality / system prompt (player-only prefix is always prepended in Python)
                <textarea
                  className="textarea"
                  rows={5}
                  value={selected.systemPrompt}
                  onChange={(e) => void updateField({ id: selected.id, systemPrompt: e.target.value })}
                />
              </label>
              <details className="details">
                <summary>Always enforced player-only prefix</summary>
                <pre className="pre">{PLAYER_ONLY_PROMPT_SNIPPET}</pre>
              </details>
              <label>
                Short-term message cap
                <input
                  type="number"
                  className="input"
                  value={selected.memoryShortTermLimit}
                  onChange={(e) =>
                    void updateField({
                      id: selected.id,
                      memoryShortTermLimit: Number(e.target.value),
                    })
                  }
                />
              </label>
              <label className="row" style={{ flexDirection: "row", gap: "0.5rem", alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={selected.memoryLongTermEnabled}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryLongTermEnabled: e.target.checked })
                  }
                />
                Long-term memory enabled (embeddings + semantic recall)
              </label>
              <label className="row" style={{ flexDirection: "row", gap: "0.5rem", alignItems: "flex-start" }}>
                <input
                  type="checkbox"
                  checked={selected.memoryLtmAgentCurated}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryLtmAgentCurated: e.target.checked })
                  }
                  disabled={!selected.memoryLongTermEnabled}
                />
                <span>
                  <strong>Character picks long-term facts</strong> — after each reply, the same model is asked (in
                  JSON) which discrete memories to store. When off, the app uses the automatic interaction summary +
                  embedding dedup instead.
                </span>
              </label>
              <label>
                Short-term memory guidance (optional, injected into system context)
                <textarea
                  className="textarea"
                  rows={2}
                  value={selected.memoryStmGuidance}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryStmGuidance: e.target.value })
                  }
                  placeholder="e.g. Prioritize recent combat and NPC names; ignore small talk."
                />
              </label>
              <label>
                Long-term memory guidance (optional, injected into system context)
                <textarea
                  className="textarea"
                  rows={2}
                  value={selected.memoryLtmGuidance}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryLtmGuidance: e.target.value })
                  }
                  placeholder="e.g. Remember facts about the character’s bonds and quests."
                />
              </label>
              <label>
                Short-term storage filter (optional — extra LLM YES/NO before saving recent dialogue)
                <textarea
                  className="textarea"
                  rows={2}
                  value={selected.memoryStmFilter}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryStmFilter: e.target.value })
                  }
                  placeholder="Leave empty to always store. If set, describes what should count as worth remembering in short-term."
                />
              </label>
              <label>
                Long-term storage filter (optional — extra LLM YES/NO before embedding to long-term)
                <textarea
                  className="textarea"
                  rows={2}
                  value={selected.memoryLtmFilter}
                  onChange={(e) =>
                    void updateField({ id: selected.id, memoryLtmFilter: e.target.value })
                  }
                  placeholder="Leave empty to use default behavior when long-term is enabled. If set, gates what gets summarized into long-term memory."
                />
              </label>
              <label>
                Embedding model (memory)
                <input
                  className="input"
                  list={`ollama-embed-${selected.id}`}
                  autoComplete="off"
                  value={selected.embeddingModel}
                  onChange={(e) => void updateField({ id: selected.id, embeddingModel: e.target.value })}
                  placeholder="nomic-embed-text"
                />
                <datalist id={`ollama-embed-${selected.id}`}>
                  {embedModelOptions.map((m) => (
                    <option key={`e-${m}`} value={m} />
                  ))}
                </datalist>
              </label>
              <label>
                Piper voice (<code>.onnx</code> filename in your Piper models folder)
                <input
                  className="input"
                  list="fas-piper-voices-merged"
                  placeholder={DEFAULT_AGENT_PIPER_ONNX}
                  autoComplete="off"
                  value={selected.voiceModel ?? ""}
                  onChange={(e) =>
                    void updateField({
                      id: selected.id,
                      voiceProvider: "piper",
                      voiceModel: e.target.value.trim() || null,
                    })
                  }
                />
                <p className="small muted" style={{ marginTop: "0.35rem" }}>
                  Suggestions list only <code>.onnx</code> files found in your effective Piper models folder (Save
                  voice paths &amp; Refresh). You can still type another filename manually.
                </p>
              </label>
              <label>
                STT provider (optional, reserved)
                <input
                  className="input"
                  placeholder="stt_provider"
                  value={selected.sttProvider ?? ""}
                  onChange={(e) =>
                    void updateField({ id: selected.id, sttProvider: e.target.value })
                  }
                />
              </label>
              <div className="row">
                <button type="button" onClick={() => void deleteAgent(selected.id)}>
                  Delete
                </button>
              </div>
            </div>
          )}
        </div>
      </section>

      {selected && (
        <>
          <section className="card">
            <h2>Test chat (Ollama)</h2>
            <div className="row" style={{ alignItems: "center", marginBottom: "0.5rem" }}>
              <label className="row" style={{ flexDirection: "row", gap: "0.5rem", margin: 0 }}>
                <input
                  type="radio"
                  name="scope"
                  checked={chatScope === "private"}
                  onChange={() => setChatScope("private")}
                />
                Private — only this agent hears (their memory only)
              </label>
              <label className="row" style={{ flexDirection: "row", gap: "0.5rem", margin: 0 }}>
                <input
                  type="radio"
                  name="scope"
                  checked={chatScope === "party"}
                  onChange={() => setChatScope("party")}
                />
                Party — every enabled agent remembers; this one replies
              </label>
            </div>
            <p className="small muted" style={{ marginBottom: "0.65rem" }}>
              <strong>Private:</strong> message goes to the selected agent only. <strong>Party:</strong> the same line
              is appended to every <em>enabled</em> agent as <code>Party: …</code>, then the selected agent generates a
              reply in character.
            </p>
            <textarea
              className="textarea"
              rows={3}
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              placeholder="Message as if you were another player or the GM…"
            />
            <div className="row">
              <button type="button" disabled={streaming} onClick={() => void runChat()}>
                {streaming ? "Streaming…" : "Send (stream)"}
              </button>
            </div>
            <div
              className="row"
              style={{ flexWrap: "wrap", gap: "0.75rem", alignItems: "flex-end", marginTop: "0.75rem" }}
            >
              <label style={{ flex: "0 0 8rem" }}>
                Banter max turns
                <input
                  type="number"
                  min={2}
                  max={12}
                  className="input"
                  value={banterMaxTurns}
                  onChange={(e) => setBanterMaxTurns(Number(e.target.value))}
                />
              </label>
              <label style={{ flex: "1 1 14rem" }}>
                Topic (optional)
                <input
                  className="input"
                  value={banterTopic}
                  onChange={(e) => setBanterTopic(e.target.value)}
                  placeholder="e.g. by the campfire…"
                />
              </label>
              <button type="button" disabled={streaming || banterBusy} onClick={() => void runBanter()}>
                {banterBusy ? "Banter…" : "Party banter (AIs)"}
              </button>
            </div>
            <p className="small muted" style={{ marginTop: "0.35rem" }}>
              <strong>Party banter</strong> runs a short round-robin between every <em>enabled</em> player agent (2–12
              total lines). Disabled while Foundry combat sync shows an active encounter. Output is capped per line;
              prompts discourage long scenes. Lines are stored as <code>Banter: …</code> in each PC&apos;s short-term
              memory.
            </p>
            <pre className="pre out">{chatOut}</pre>
          </section>

          <section className="card">
            <h2>Voice conversation (Whisper)</h2>
            <p className="small muted">
              Uses the same <strong>Private / Party</strong> setting as Test chat above. Speech is transcribed with
              whisper-cli, then the same memory + Ollama flow runs. Replies can be spoken (Piper if configured, else
              browser TTS).
            </p>
            <label className="row" style={{ flexDirection: "row", gap: "0.5rem", marginTop: "0.5rem" }}>
              <input
                type="checkbox"
                checked={speakReplies}
                onChange={(e) => setSpeakReplies(e.target.checked)}
              />
              Speak agent replies aloud
            </label>
            <div className="row" style={{ marginTop: "0.75rem", flexWrap: "wrap" }}>
              <button
                type="button"
                className={voiceRecording ? "active" : ""}
                disabled={voiceConvBusy || streaming || !whisperReady}
                onMouseDown={() => void onWhisperDown()}
                onMouseUp={() => void onWhisperUp()}
                onMouseLeave={() => {
                  if (voiceRecording) void onWhisperUp();
                }}
              >
                {voiceRecording ? "Recording…" : "Hold to speak (Whisper)"}
              </button>
              <span className="muted small">
                {whisperReady
                  ? "WAV → whisper-cli → chat. Release to send."
                  : "Set whisper path + model in Local voice, then Save & Refresh."}
              </span>
            </div>
            <p className="muted small" style={{ marginTop: "0.5rem" }}>
              Piper voice for replies:{" "}
              <code>{agentPiperOnnxFile(selected, piperTestModel)}</code>
              {selected.voiceModel?.trim()
                ? " (this agent)"
                : " (Local voice test model — set Piper voice on the agent)"}
              .
            </p>
          </section>

          <section className="card">
            <h2>Memory (embeddings)</h2>
            <textarea
              className="textarea"
              rows={2}
              value={memQuery}
              onChange={(e) => setMemQuery(e.target.value)}
              placeholder="Text to search or store as long-term memory"
            />
            <div className="row">
              <button type="button" onClick={() => void memorySearch()}>
                Semantic search
              </button>
              <button type="button" onClick={() => void memoryEmbed()}>
                Embed &amp; store
              </button>
            </div>
            <ul className="hits">
              {memHits.map((h) => (
                <li key={h.slice(0, 40)}>{h}</li>
              ))}
            </ul>
          </section>

          <section className="card">
            <h2>Voice — quick test</h2>
            <p className="small muted">
              Mock TTS only. Real voice chat is in <strong>Voice conversation</strong> above.
            </p>
            <div className="row">
              <button type="button" onClick={() => void testVoice()}>
                Test mock TTS
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
