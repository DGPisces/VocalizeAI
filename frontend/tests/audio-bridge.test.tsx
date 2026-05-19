import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";
import {
  BrowserAudioBridge,
  type AudioBridgeState
} from "../components/BrowserAudioBridge";
import type { ServerFrame } from "../lib/ws";

type BridgeTestHandle = {
  feedAudio: (frame: { role: string; pcm: Uint8Array }) => {
    scheduled: boolean;
    queuedSeconds: number;
  };
  handleFrame: (frame: ServerFrame) => void;
  getState: () => AudioBridgeState;
};

function getTestHandle(): BridgeTestHandle {
  const handle = (
    BrowserAudioBridge as unknown as { __test_handle__?: BridgeTestHandle }
  ).__test_handle__;
  if (!handle) {
    throw new Error("BrowserAudioBridge.__test_handle__ not attached");
  }
  return handle;
}

describe("BrowserAudioBridge", () => {
  it("requests desktop Chrome echo cancellation when audio starts", async () => {
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    render(<BrowserAudioBridge sendAudio={vi.fn()} onStatusChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));

    await screen.findByText("Connected");
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
  });

  it("requests the selected microphone when inputDeviceId is provided", async () => {
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        inputDeviceId="mic-2"
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));

    await screen.findByText("Connected");
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        deviceId: { exact: "mic-2" },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
  });

  it("omits deviceId for the default microphone and honors disabled AEC", async () => {
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        inputDeviceId=""
        echoCancellation={false}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));

    await screen.findByText("Connected");
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
  });

  it("does not open multiple concurrent capture pipelines", async () => {
    let processor: { onaudioprocess: ((event: any) => void) | null } | null = null;
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });

    class FakeAudioContext {
      sampleRate = 48000;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        processor = {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        } as any;
        return processor;
      }
      close() {
        return Promise.resolve();
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    render(<BrowserAudioBridge sendAudio={vi.fn()} onStatusChange={vi.fn()} />);
    const startButton = screen.getByRole("button", { name: /start audio/i });
    fireEvent.click(startButton);
    fireEvent.click(startButton);

    await waitFor(() => expect(processor).not.toBeNull());
    expect(getUserMedia).toHaveBeenCalledTimes(1);
  });

  it("releases the media stream when AudioContext is unavailable", async () => {
    const sendAudio = vi.fn();
    const stop = vi.fn();
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: undefined
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop }]
        })
      }
    });

    render(<BrowserAudioBridge sendAudio={sendAudio} onStatusChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));

    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
    expect(sendAudio).not.toHaveBeenCalled();
  });

  it("shows permission errors with role alert", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockRejectedValue(new Error("denied")) }
    });

    render(<BrowserAudioBridge sendAudio={vi.fn()} onStatusChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Microphone unavailable");
  });

  it("checks current bufferedAmount inside every capture callback", async () => {
    const sendAudio = vi.fn();
    const onStatusChange = vi.fn();
    let bufferedAmount = 0;
    let processor: { onaudioprocess: ((event: any) => void) | null } | null = null;

    class FakeAudioContext {
      sampleRate = 48000;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        processor = {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        } as any;
        return processor;
      }
      close() {
        return Promise.resolve();
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }]
        })
      }
    });

    render(
      <BrowserAudioBridge
        sendAudio={sendAudio}
        getBufferedAmount={() => bufferedAmount}
        onStatusChange={onStatusChange}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));
    await waitFor(() => expect(processor).not.toBeNull());
    await screen.findByText("Connected");

    bufferedAmount = 700 * 1024;
    act(() => {
      processor!.onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
      });
    });

    expect(sendAudio).not.toHaveBeenCalled();
    expect(onStatusChange).toHaveBeenCalledWith(expect.objectContaining({
      degraded: true
    }));
  });

  it("resumes capture after transient backpressure drops frames", async () => {
    const sendAudio = vi.fn();
    let bufferedAmount = 700 * 1024;
    let processor: { onaudioprocess: ((event: any) => void) | null } | null = null;

    class FakeAudioContext {
      sampleRate = 48000;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        processor = {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        } as any;
        return processor;
      }
      close() {
        return Promise.resolve();
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }]
        })
      }
    });

    render(
      <BrowserAudioBridge
        sendAudio={sendAudio}
        getBufferedAmount={() => bufferedAmount}
        onStatusChange={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));
    await waitFor(() => expect(processor).not.toBeNull());

    for (let i = 0; i < 51; i += 1) {
      act(() => {
        processor!.onaudioprocess?.({
          inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
        });
      });
    }
    expect(sendAudio).not.toHaveBeenCalled();

    bufferedAmount = 0;
    act(() => {
      processor!.onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
      });
    });

    expect(sendAudio).toHaveBeenCalledTimes(1);
  });

  it("marks capture degraded when audio cannot be sent for over one second", async () => {
    const sendAudio = vi.fn().mockReturnValue(false);
    const onStatusChange = vi.fn();
    let processor: { onaudioprocess: ((event: any) => void) | null } | null = null;

    class FakeAudioContext {
      sampleRate = 48000;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        processor = {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        } as any;
        return processor;
      }
      close() {
        return Promise.resolve();
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({
          getTracks: () => [{ stop: vi.fn() }]
        })
      }
    });

    render(
      <BrowserAudioBridge
        sendAudio={sendAudio}
        getBufferedAmount={() => 0}
        onStatusChange={onStatusChange}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: /start audio/i }));
    await waitFor(() => expect(processor).not.toBeNull());

    for (let i = 0; i < 51; i += 1) {
      act(() => {
        processor!.onaudioprocess?.({
          inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
        });
      });
    }

    expect(onStatusChange).toHaveBeenCalledWith(expect.objectContaining({
      degraded: true
    }));
  });

  it("schedules incoming playback frames in order", () => {
    const starts: number[] = [];

    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return {
          duration: length / sampleRate,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: (when: number) => starts.push(when)
        };
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    const { rerender } = render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0, 0, 0]) }}
      />
    );
    rerender(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0, 0, 0]) }}
      />
    );

    expect(starts).toHaveLength(2);
    expect(starts[0]).toBe(10);
    expect(starts[1]).toBeGreaterThan(starts[0]);
  });

  it("resumes a suspended playback context before scheduling audio", () => {
    const resumeAudio = vi.fn().mockResolvedValue(undefined);
    const starts: number[] = [];

    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      state = "suspended";
      resume = resumeAudio;
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return {
          duration: length / sampleRate,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: (when: number) => starts.push(when)
        };
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0]) }}
      />
    );

    expect(resumeAudio).toHaveBeenCalledTimes(1);
    expect(starts).toHaveLength(1);
  });

  it("marks playback degraded when queued audio exceeds five seconds", () => {
    const onStatusChange = vi.fn();

    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      createBuffer() {
        return {
          duration: 6,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: vi.fn()
        };
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={onStatusChange}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0]) }}
      />
    );

    expect(onStatusChange).toHaveBeenCalledWith(expect.objectContaining({
      degraded: true
    }));
  });

  it("schedules a long playback frame only once when degraded status changes", async () => {
    const starts: number[] = [];
    const onStatusChange = vi.fn();

    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      createBuffer() {
        return {
          duration: 6,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: (when: number) => starts.push(when)
        };
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={onStatusChange}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0]) }}
      />
    );

    await waitFor(() => expect(onStatusChange).toHaveBeenCalledWith(
      expect.objectContaining({ degraded: true })
    ));
    expect(starts).toHaveLength(1);
  });

  it("closes playback context on unmount even when capture never started", () => {
    const close = vi.fn().mockResolvedValue(undefined);

    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      close() {
        return close();
      }
      createBuffer() {
        return {
          duration: 0.1,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: vi.fn()
        };
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    const { unmount } = render(
      <BrowserAudioBridge
        sendAudio={vi.fn()}
        onStatusChange={vi.fn()}
        playbackFrame={{ role: "ai_to_user", pcm: new Uint8Array([0, 0]) }}
      />
    );

    unmount();

    expect(close).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// B3a state-machine tests (Plan B3a-ui Phase B: B1 + B2 + B3).
// ---------------------------------------------------------------------------

function installPlaybackContext() {
  class FakeAudioContext {
    currentTime = 10;
    destination = {};
    createBuffer(_channels: number, length: number, sampleRate: number) {
      return {
        duration: length / sampleRate,
        copyToChannel: vi.fn()
      };
    }
    createBufferSource() {
      return {
        buffer: null,
        connect: vi.fn(),
        start: vi.fn()
      };
    }
    close() {
      return Promise.resolve();
    }
  }
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: FakeAudioContext
  });
}

function installCaptureContext(
  processors: Array<{ onaudioprocess: ((event: any) => void) | null }>
) {
  class FakeAudioContext {
    sampleRate = 48000;
    destination = {};
    createMediaStreamSource() {
      return { connect: vi.fn(), disconnect: vi.fn() };
    }
    createScriptProcessor() {
      const processor = {
        onaudioprocess: null,
        connect: vi.fn(),
        disconnect: vi.fn()
      };
      processors.push(processor);
      return processor as any;
    }
    close() {
      return Promise.resolve();
    }
  }
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: FakeAudioContext
  });
}

describe("BrowserAudioBridge B3a state machine", () => {
  it("starts in idle and stays there until readiness passes", () => {
    const onState = vi.fn();
    render(<BrowserAudioBridge onState={onState} />);
    expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>("idle");
  });

  it("transitions idle -> handover_ready on readiness pass", async () => {
    const onState = vi.fn();
    const { rerender } = render(<BrowserAudioBridge onState={onState} />);
    rerender(<BrowserAudioBridge onState={onState} readinessPassed />);
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "handover_ready"
      );
    });
  });

  it("does NOT request getUserMedia until handover_ready -> call_listening", async () => {
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onState = vi.fn();
    const { rerender } = render(<BrowserAudioBridge onState={onState} />);
    rerender(<BrowserAudioBridge onState={onState} readinessPassed />);
    expect(getUserMedia).not.toHaveBeenCalled();
    rerender(
      <BrowserAudioBridge onState={onState} readinessPassed handover />
    );
    await waitFor(() => {
      expect(getUserMedia).toHaveBeenCalledTimes(1);
    });
  });

  it("stops managed mic capture when leaving active call phases", async () => {
    const stop = vi.fn();
    const getUserMedia = vi.fn().mockResolvedValue({
      getTracks: () => [{ stop }]
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    class FakeAudioContext {
      sampleRate = 48000;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        return {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        } as any;
      }
      close() {
        return Promise.resolve();
      }
    }
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });

    const { rerender } = render(
      <BrowserAudioBridge readinessPassed handover socket={{} as any} />
    );
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));

    rerender(<BrowserAudioBridge readinessPassed socket={{} as any} />);

    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
  });

  it("clears switching state when a pending device switch is cancelled", async () => {
    installCaptureContext([]);
    let resolveSwitch: ((stream: MediaStream) => void) | undefined;
    const getUserMedia = vi
      .fn()
      .mockResolvedValueOnce({ getTracks: () => [{ stop: vi.fn() }] })
      .mockImplementationOnce(() => new Promise<MediaStream>((resolve) => {
        resolveSwitch = resolve;
      }));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onDeviceSwitchingChange = vi.fn();

    const { rerender, unmount } = render(
      <BrowserAudioBridge
        readinessPassed
        handover
        inputDeviceId="mic-1"
        onDeviceSwitchingChange={onDeviceSwitchingChange}
      />
    );
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));

    rerender(
      <BrowserAudioBridge
        readinessPassed
        handover
        inputDeviceId="mic-2"
        onDeviceSwitchingChange={onDeviceSwitchingChange}
      />
    );
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
    expect(onDeviceSwitchingChange).toHaveBeenCalledWith(true);

    unmount();
    resolveSwitch?.({ getTracks: () => [{ stop: vi.fn() }] } as unknown as MediaStream);

    await waitFor(() => {
      expect(onDeviceSwitchingChange).toHaveBeenLastCalledWith(false);
    });
  });

  it("never enters preflight_listening or preflight_ai_speaking", async () => {
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onState = vi.fn();
    const { rerender } = render(<BrowserAudioBridge onState={onState} />);
    rerender(
      <BrowserAudioBridge onState={onState} readinessPassed handover />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenCalledWith<[AudioBridgeState]>(
        "call_listening"
      );
    });
    rerender(
      <BrowserAudioBridge
        onState={onState}
        readinessPassed
        handover
        takeoverActive
      />
    );
    rerender(<BrowserAudioBridge onState={onState} ended />);
    const seen = onState.mock.calls.map((c) => c[0]);
    expect(seen).not.toContain("preflight_listening");
    expect(seen).not.toContain("preflight_ai_speaking");
    expect(seen).toContain("idle");
    expect(seen).toContain("call_listening");
    expect(seen).toContain("user_takeover");
    expect(seen).toContain("ended");
  });

  it("mutes outbound playback when in user_takeover", async () => {
    installPlaybackContext();
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onState = vi.fn();
    const { rerender } = render(
      <BrowserAudioBridge onState={onState} readinessPassed handover />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "call_listening"
      );
    });
    const handle = getTestHandle();

    const first = handle.feedAudio({
      role: "ai_to_user",
      pcm: new Uint8Array(320)
    });
    expect(first.scheduled).toBe(true);

    rerender(
      <BrowserAudioBridge
        onState={onState}
        readinessPassed
        handover
        takeoverActive
      />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "user_takeover"
      );
    });

    const second = handle.feedAudio({
      role: "ai_to_user",
      pcm: new Uint8Array(320)
    });
    expect(second.scheduled).toBe(false);
  });

  it("plays ai_to_merchant frames during normal speakerphone flow", async () => {
    installPlaybackContext();
    const onState = vi.fn();
    render(<BrowserAudioBridge onState={onState} readinessPassed handover />);
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "call_listening"
      );
    });

    const result = getTestHandle().feedAudio({
      role: "ai_to_merchant",
      pcm: new Uint8Array(320)
    });

    expect(result.scheduled).toBe(true);
  });

  it("suppresses microphone uplink while local playback can echo into capture", async () => {
    const sendAudio = vi.fn();
    const processors: Array<{ onaudioprocess: ((event: any) => void) | null }> = [];
    const getUserMedia = vi.fn().mockResolvedValue({
      getTracks: () => [{ stop: vi.fn() }]
    });

    class FakeAudioContext {
      sampleRate = 48000;
      currentTime = 10;
      destination = {};
      createMediaStreamSource() {
        return { connect: vi.fn(), disconnect: vi.fn() };
      }
      createScriptProcessor() {
        const processor = {
          onaudioprocess: null,
          connect: vi.fn(),
          disconnect: vi.fn()
        };
        processors.push(processor);
        return processor as any;
      }
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return {
          duration: length / sampleRate,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: vi.fn()
        };
      }
      close() {
        return Promise.resolve();
      }
    }

    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });

    render(
      <BrowserAudioBridge
        sendAudio={sendAudio}
        onStatusChange={vi.fn()}
        readinessPassed
        handover
      />
    );
    await waitFor(() => expect(processors).toHaveLength(1));

    const result = getTestHandle().feedAudio({
      role: "ai_to_merchant",
      pcm: new Uint8Array(320)
    });
    expect(result.scheduled).toBe(true);

    act(() => {
      processors[0].onaudioprocess?.({
        inputBuffer: {
          getChannelData: () => new Float32Array([0.1, 0.2, 0.3])
        }
      });
    });

    expect(sendAudio).not.toHaveBeenCalled();
  });

  it("plays ai_to_merchant frames during user takeover typed passthrough", async () => {
    installPlaybackContext();
    const onState = vi.fn();
    render(
      <BrowserAudioBridge
        onState={onState}
        readinessPassed
        handover
        takeoverActive
      />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "user_takeover"
      );
    });

    const result = getTestHandle().feedAudio({
      role: "ai_to_merchant",
      pcm: new Uint8Array(320)
    });

    expect(result.scheduled).toBe(true);
  });

  it("follows mode_ack(call_speaking) into call_speaking state", async () => {
    installPlaybackContext();
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onState = vi.fn();
    render(
      <BrowserAudioBridge onState={onState} readinessPassed handover />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "call_listening"
      );
    });
    const handle = getTestHandle();
    act(() => {
      handle.handleFrame({ type: "mode_ack", mode: "call_speaking" });
    });
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "call_speaking"
      );
    });
  });

  it("mode_ack(call_listening) does not override active user_takeover", async () => {
    installPlaybackContext();
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onState = vi.fn();
    render(
      <BrowserAudioBridge
        onState={onState}
        readinessPassed
        handover
        takeoverActive
      />
    );
    await waitFor(() => {
      expect(onState).toHaveBeenLastCalledWith<[AudioBridgeState]>(
        "user_takeover"
      );
    });
    const handle = getTestHandle();
    act(() => {
      handle.handleFrame({ type: "mode_ack", mode: "call_listening" });
    });
    expect(handle.getState()).toBe("user_takeover");
  });
});

describe("BrowserAudioBridge device switching", () => {
  it("pauses merchant audio sends while active capture restarts and resumes after success", async () => {
    const processors: Array<{ onaudioprocess: ((event: any) => void) | null }> = [];
    installCaptureContext(processors);
    const firstStop = vi.fn();
    const secondStop = vi.fn();
    let resolveSecondCapture: ((stream: { getTracks: () => Array<{ stop: () => void }> }) => void) | null = null;
    const getUserMedia = vi
      .fn()
      .mockResolvedValueOnce({ getTracks: () => [{ stop: firstStop }] })
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveSecondCapture = resolve;
      }));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const sendAudio = vi.fn().mockReturnValue(true);
    const onDeviceSwitchingChange = vi.fn();
    const onDeviceSwitchSuccess = vi.fn();
    const { rerender } = render(
      <BrowserAudioBridge
        readinessPassed
        handover
        sendAudio={sendAudio}
        inputDeviceId="mic-1"
        echoCancellation
        onDeviceSwitchingChange={onDeviceSwitchingChange}
        onDeviceSwitchSuccess={onDeviceSwitchSuccess}
      />
    );

    await waitFor(() => expect(processors).toHaveLength(1));
    rerender(
      <BrowserAudioBridge
        readinessPassed
        handover
        sendAudio={sendAudio}
        inputDeviceId="mic-2"
        echoCancellation={false}
        onDeviceSwitchingChange={onDeviceSwitchingChange}
        onDeviceSwitchSuccess={onDeviceSwitchSuccess}
      />
    );
    await waitFor(() => expect(onDeviceSwitchingChange).toHaveBeenCalledWith(true));

    act(() => {
      processors[0].onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
      });
    });
    expect(sendAudio).not.toHaveBeenCalled();
    expect(firstStop).toHaveBeenCalledTimes(1);

    act(() => {
      resolveSecondCapture?.({ getTracks: () => [{ stop: secondStop }] });
    });
    await waitFor(() => expect(onDeviceSwitchSuccess).toHaveBeenCalledTimes(1));
    expect(onDeviceSwitchingChange).toHaveBeenLastCalledWith(false);

    act(() => {
      processors[1].onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array([0.1, 0.2, 0.3]) }
      });
    });
    expect(sendAudio).toHaveBeenCalledTimes(1);
    expect(getUserMedia).toHaveBeenNthCalledWith(2, {
      audio: {
        channelCount: 1,
        deviceId: { exact: "mic-2" },
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
  });

  it("rolls back to the last working capture preferences when switching fails", async () => {
    const processors: Array<{ onaudioprocess: ((event: any) => void) | null }> = [];
    installCaptureContext(processors);
    const getUserMedia = vi
      .fn()
      .mockResolvedValueOnce({ getTracks: () => [{ stop: vi.fn() }] })
      .mockRejectedValueOnce(new Error("denied"))
      .mockResolvedValueOnce({ getTracks: () => [{ stop: vi.fn() }] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const onDeviceSwitchError = vi.fn();
    const onDeviceSwitchSuccess = vi.fn();
    const { rerender } = render(
      <BrowserAudioBridge
        readinessPassed
        handover
        sendAudio={vi.fn()}
        inputDeviceId="mic-1"
        echoCancellation
        onDeviceSwitchError={onDeviceSwitchError}
        onDeviceSwitchSuccess={onDeviceSwitchSuccess}
      />
    );

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    rerender(
      <BrowserAudioBridge
        readinessPassed
        handover
        sendAudio={vi.fn()}
        inputDeviceId="mic-2"
        echoCancellation={false}
        onDeviceSwitchError={onDeviceSwitchError}
        onDeviceSwitchSuccess={onDeviceSwitchSuccess}
      />
    );

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(3));
    expect(getUserMedia).toHaveBeenNthCalledWith(2, {
      audio: {
        channelCount: 1,
        deviceId: { exact: "mic-2" },
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
    expect(getUserMedia).toHaveBeenNthCalledWith(3, {
      audio: {
        channelCount: 1,
        deviceId: { exact: "mic-1" },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    });
    expect(onDeviceSwitchError).toHaveBeenCalledWith("device_settings.switch_error");
    expect(onDeviceSwitchSuccess).not.toHaveBeenCalled();
  });

  it("routes playback to the selected speaker when AudioContext setSinkId is supported", async () => {
    const setSinkId = vi.fn().mockResolvedValue(undefined);
    class FakeAudioContext {
      currentTime = 10;
      destination = {};
      setSinkId = setSinkId;
      createBuffer(_channels: number, length: number, sampleRate: number) {
        return {
          duration: length / sampleRate,
          copyToChannel: vi.fn()
        };
      }
      createBufferSource() {
        return {
          buffer: null,
          connect: vi.fn(),
          start: vi.fn()
        };
      }
      close() {
        return Promise.resolve();
      }
    }
    Object.defineProperty(window, "AudioContext", {
      configurable: true,
      value: FakeAudioContext
    });
    render(<BrowserAudioBridge outputDeviceId="spk-1" onSpeakerFallback={vi.fn()} />);
    await waitFor(() => expect(getTestHandle()).toBeTruthy());

    const result = getTestHandle().feedAudio({
      role: "ai_to_merchant",
      pcm: new Uint8Array(320)
    });

    expect(result.scheduled).toBe(true);
    await waitFor(() => expect(setSinkId).toHaveBeenCalledWith("spk-1"));
  });

  it("keeps playback working and reports fallback when speaker routing is unsupported", async () => {
    installPlaybackContext();
    const onSpeakerFallback = vi.fn();
    render(
      <BrowserAudioBridge
        outputDeviceId="spk-1"
        onSpeakerFallback={onSpeakerFallback}
      />
    );
    await waitFor(() => expect(getTestHandle()).toBeTruthy());

    const result = getTestHandle().feedAudio({
      role: "ai_to_merchant",
      pcm: new Uint8Array(320)
    });

    expect(result.scheduled).toBe(true);
    expect(onSpeakerFallback).toHaveBeenCalledTimes(1);
  });
});
