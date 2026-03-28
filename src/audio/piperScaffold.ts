import type { TTSProvider, VoiceInfo, VoiceSettings } from "./types";

/**
 * Placeholder — use `/api/voice/piper/synthesize` and local paths in the UI (see README).
 */
export class PiperTtsScaffold implements TTSProvider {
  async listVoices(): Promise<VoiceInfo[]> {
    return [];
  }

  async synthesize(_text: string, _settings: VoiceSettings): Promise<Uint8Array> {
    throw new Error("Piper TTS — configure binary + models in app settings (Python backend).");
  }

  async speak(_text: string, _settings: VoiceSettings): Promise<void> {
    throw new Error("Piper TTS not wired");
  }
}
