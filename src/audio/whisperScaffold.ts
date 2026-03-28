import type { STTProvider } from "./types";

/**
 * Placeholder — use `/api/voice/whisper/transcribe` and paths in the UI (see README).
 */
export class WhisperSttScaffold implements STTProvider {
  async startListening(): Promise<void> {}

  async stopListening(): Promise<void> {}

  async transcribe(_audio: Uint8Array): Promise<string> {
    throw new Error("Whisper STT — point whisper binary in settings (Python backend).");
  }
}
