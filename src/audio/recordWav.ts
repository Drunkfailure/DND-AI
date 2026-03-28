/**
 * Capture microphone to 16-bit PCM WAV mono @ 16 kHz (whisper.cpp-friendly).
 */

function resample(buffer: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return buffer;
  const ratio = fromRate / toRate;
  const newLength = Math.max(1, Math.round(buffer.length / ratio));
  const out = new Float32Array(newLength);
  for (let i = 0; i < newLength; i++) {
    const src = i * ratio;
    const i0 = Math.floor(src);
    const i1 = Math.min(i0 + 1, buffer.length - 1);
    const t = src - i0;
    out[i] = buffer[i0] * (1 - t) + buffer[i1] * t;
  }
  return out;
}

function encodeWavPcm16(samples: Float32Array, sampleRate: number): Uint8Array {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++, off += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(buffer);
}

const TARGET_RATE = 16000;

export class WavRecorder {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private processor: ScriptProcessorNode | null = null;
  private chunks: Float32Array[] = [];
  private sourceRate = TARGET_RATE;

  async start(): Promise<void> {
    this.chunks = [];
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    this.stream = stream;
    const ctx = new AudioContext();
    this.ctx = ctx;
    this.sourceRate = ctx.sampleRate;
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    this.processor = processor;
    processor.onaudioprocess = (e) => {
      const ch = e.inputBuffer.getChannelData(0);
      this.chunks.push(new Float32Array(ch));
    };
    const gain = ctx.createGain();
    gain.gain.value = 0;
    source.connect(processor);
    processor.connect(gain);
    gain.connect(ctx.destination);
  }

  async stop(): Promise<Uint8Array> {
    const ctx = this.ctx;
    const stream = this.stream;
    const processor = this.processor;
    this.ctx = null;
    this.stream = null;
    this.processor = null;
    if (processor) {
      processor.disconnect();
      processor.onaudioprocess = null;
    }
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (ctx) await ctx.close();

    let length = 0;
    for (const c of this.chunks) length += c.length;
    const merged = new Float32Array(length);
    let off = 0;
    for (const c of this.chunks) {
      merged.set(c, off);
      off += c.length;
    }
    this.chunks = [];
    if (merged.length === 0) {
      return encodeWavPcm16(new Float32Array(1), TARGET_RATE);
    }
    const samples = resample(merged, this.sourceRate, TARGET_RATE);
    return encodeWavPcm16(samples, TARGET_RATE);
  }
}

export function uint8ToBase64(u8: Uint8Array): string {
  let binary = "";
  const chunk = 8192;
  for (let i = 0; i < u8.length; i += chunk) {
    binary += String.fromCharCode(...u8.subarray(i, i + chunk));
  }
  return btoa(binary);
}
