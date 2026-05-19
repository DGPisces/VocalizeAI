export const INPUT_SAMPLE_RATE = 16_000;
export const OUTPUT_SAMPLE_RATE = 24_000;
export const PREFERRED_CHUNK_MS = 20;
export const MAX_BUFFERED_AMOUNT_BYTES = 512 * 1024;
export const MAX_PENDING_CAPTURE_MS = 1000;

export function floatToPcm16(samples: Float32Array): Uint8Array {
  const out = new Uint8Array(samples.length * 2);
  const view = new DataView(out.buffer);
  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample));
    const value = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    view.setInt16(index * 2, value, true);
  });
  return out;
}

export function pcm16ToFloat32(bytes: Uint8Array): Float32Array {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Float32Array(bytes.byteLength / 2);
  for (let i = 0; i < out.length; i += 1) {
    const sample = view.getInt16(i * 2, true);
    out[i] = sample < 0 ? sample / 0x8000 : sample / 0x7fff;
  }
  return out;
}

export function downsampleTo16k(input: Float32Array, sourceRate: number): Float32Array {
  if (sourceRate === INPUT_SAMPLE_RATE) {
    return input;
  }
  const ratio = sourceRate / INPUT_SAMPLE_RATE;
  const outputLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.max(start + 1, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end && j < input.length; j += 1) {
      sum += input[j];
    }
    out[i] = sum / Math.max(1, Math.min(end, input.length) - start);
  }
  return out;
}

export function calculateRms(samples: Float32Array): number {
  if (samples.length === 0) {
    return 0;
  }
  let sumSquares = 0;
  for (const sample of samples) {
    sumSquares += sample * sample;
  }
  return Math.sqrt(sumSquares / samples.length);
}

export function shouldDegradeCapture({
  bufferedAmount,
  pendingMs
}: {
  bufferedAmount: number;
  pendingMs: number;
}): boolean {
  return bufferedAmount > MAX_BUFFERED_AMOUNT_BYTES || pendingMs > MAX_PENDING_CAPTURE_MS;
}
