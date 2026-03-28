import { apiBase, apiJson } from "../api";
import type { STTProvider, TTSProvider, VoiceInfo, VoiceSettings } from "./types";

function toB64(u8: Uint8Array): string {
  let binary = "";
  const chunk = 8192;
  for (let i = 0; i < u8.length; i += chunk) {
    binary += String.fromCharCode(...u8.subarray(i, i + chunk));
  }
  return btoa(binary);
}

export class MockTTSProvider implements TTSProvider {
  async listVoices(): Promise<VoiceInfo[]> {
    return apiJson<VoiceInfo[]>("/api/voice/mock/voices");
  }

  async synthesize(text: string, settings: VoiceSettings): Promise<Uint8Array> {
    const j = await apiJson<{ bytesBase64: string }>("/api/voice/mock/synthesize", {
      method: "POST",
      body: JSON.stringify({ text, settings }),
    });
    const bin = atob(j.bytesBase64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async speak(text: string, settings: VoiceSettings): Promise<void> {
    const r = await fetch(`${apiBase()}/api/voice/mock/speak`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, settings }),
    });
    if (!r.ok) throw new Error(await r.text());
  }
}

export class MockSTTProvider implements STTProvider {
  async startListening(): Promise<void> {}

  async stopListening(): Promise<void> {}

  async transcribe(audio: Uint8Array): Promise<string> {
    const j = await apiJson<{ text: string }>("/api/voice/mock/stt/transcribe", {
      method: "POST",
      body: JSON.stringify({ audioBase64: toB64(audio) }),
    });
    return j.text;
  }
}
