/**
 * Frontend mirrors the native voice abstraction: swap implementations without UI churn.
 */

export interface VoiceSettings {
  voiceId?: string;
  rate?: number;
  pitch?: number;
}

export interface VoiceInfo {
  id: string;
  name: string;
  provider: string;
}

export interface TTSProvider {
  listVoices(): Promise<VoiceInfo[]>;
  synthesize(text: string, settings: VoiceSettings): Promise<Uint8Array>;
  speak(text: string, settings: VoiceSettings): Promise<void>;
}

export interface STTProvider {
  startListening(): Promise<void>;
  stopListening(): Promise<void>;
  transcribe(audio: Uint8Array): Promise<string>;
}
