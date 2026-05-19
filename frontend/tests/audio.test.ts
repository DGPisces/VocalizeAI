import { describe, expect, it } from "vitest";
import {
  calculateRms,
  downsampleTo16k,
  floatToPcm16,
  pcm16ToFloat32,
  shouldDegradeCapture
} from "../lib/audio";

describe("audio helpers", () => {
  it("converts Float32 samples to PCM16 little endian", () => {
    expect(Array.from(floatToPcm16(new Float32Array([-1, 0, 1])))).toEqual([
      0,
      128,
      0,
      0,
      255,
      127
    ]);
  });

  it("converts PCM16 bytes back to Float32", () => {
    const out = pcm16ToFloat32(new Uint8Array([0, 128, 0, 0, 255, 127]));
    expect(Array.from(out).map((n) => Number(n.toFixed(3)))).toEqual([-1, 0, 1]);
  });

  it("downsamples 48k to 16k by averaging source windows", () => {
    const input = new Float32Array([0, 0.3, 0.6, 0.9, 0.6, 0.3]);
    const out = downsampleTo16k(input, 48000);
    expect(out.length).toBe(2);
    expect(Number(out[0].toFixed(2))).toBe(0.3);
    expect(Number(out[1].toFixed(2))).toBe(0.6);
  });

  it("calculates RMS energy", () => {
    expect(Number(calculateRms(new Float32Array([1, -1, 1, -1])).toFixed(2))).toBe(1);
  });

  it("marks capture degraded when WS bufferedAmount is high", () => {
    expect(shouldDegradeCapture({ bufferedAmount: 600 * 1024, pendingMs: 20 })).toBe(true);
    expect(shouldDegradeCapture({ bufferedAmount: 10, pendingMs: 2000 })).toBe(true);
    expect(shouldDegradeCapture({ bufferedAmount: 10, pendingMs: 20 })).toBe(false);
  });
});
