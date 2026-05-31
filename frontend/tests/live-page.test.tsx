// frontend/tests/live-page.test.tsx
//
// Tests for the new Hybrid C live page (Tasks G1 + G2).
// Verifies phase-driven content swap (PreflightChat / HandoverPanel /
// TranscriptStream / PostCallReview) and the WS frame senders wired into
// HandoverPanel, getSession on mount, etc.

import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";
import { LivePageClient } from "../app/[locale]/live/[session]/LivePageClient";
import { BrowserAudioBridge } from "../components/BrowserAudioBridge";
import type {
  DecodedAudioFrame,
  ClientFrame,
  ServerFrame,
  TaskPhaseValue,
  SocketHandlers,
} from "../lib/ws";
import type {
  GetSessionResponse,
} from "../lib/api";

const mockDevices: MediaDeviceInfo[] = [
  {
    deviceId: "mic-1",
    kind: "audioinput",
    label: "Built-in Microphone",
    groupId: "grp-1",
    toJSON: () => ({}),
  },
  {
    deviceId: "mic-2",
    kind: "audioinput",
    label: "USB Microphone",
    groupId: "grp-2",
    toJSON: () => ({}),
  },
  {
    deviceId: "spk-1",
    kind: "audiooutput",
    label: "Built-in Speaker",
    groupId: "grp-3",
    toJSON: () => ({}),
  },
];

// router helper is mocked at the module level so the client component
// can import useRouter / useSearchParams without crashing under jsdom.
const replaceMock = vi.fn();
vi.mock("@/src/router", () => ({
  useRouter: () => ({ replace: replaceMock, push: replaceMock }),
  useSearchParams: () => new URLSearchParams("ws=ws://example.test/ws/sessions/s-1"),
  usePathname: () => "/zh/live/s-1",
}));

interface FakeSocket {
  sent: ClientFrame[];
  emit: (frame: ServerFrame) => void;
  emitAudio: (frame: DecodedAudioFrame) => void;
  reconnectAttempt: () => void;
  reconnected: () => void;
  closed: boolean;
}

function makeFakeSocket(): {
  socket: {
    connect: () => void;
    close: () => void;
    send: (f: ClientFrame) => void;
    sendAudio: (pcm: Uint8Array) => boolean;
    bufferedAmount: () => number;
  };
  fake: FakeSocket;
  setHandlers: (h: SocketHandlers) => void;
  ready: () => Promise<void>;
} {
  let handlers: SocketHandlers | null = null;
  let resolveReady: (() => void) | null = null;
  const readyPromise = new Promise<void>((r) => {
    resolveReady = r;
  });
  const fake: FakeSocket = {
    sent: [],
    emit: (frame) => handlers?.onFrame(frame),
    emitAudio: (frame) => handlers?.onAudio(frame),
    reconnectAttempt: () => handlers?.onReconnectAttempt?.(),
    reconnected: () => handlers?.onReconnected?.(),
    closed: false,
  };
  return {
    socket: {
      connect: () => undefined,
      close: () => {
        fake.closed = true;
      },
      send: (f) => {
        fake.sent.push(f);
      },
      sendAudio: () => true,
      bufferedAmount: () => 0,
    },
    fake,
    setHandlers: (h) => {
      handlers = h;
      resolveReady?.();
    },
    ready: () => readyPromise,
  };
}

function defaultGetSessionResponse(
  phase: TaskPhaseValue = "draft",
): GetSessionResponse {
  return {
    session_id: "s-1",
    default_lang: "zh",
    task_description: null,
    preferred_voice_id: null,
    auto_translate_merchant: true,
    phase,
    uncertain_assumptions: [],
    pending_callbacks: [],
  };
}

function wrap(ui: React.ReactNode) {
  return (
    <I18nProvider locale="zh" messages={zh}>
      {ui}
    </I18nProvider>
  );
}

function installPlaybackContext() {
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
    close() {
      return Promise.resolve();
    }
  }
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: FakeAudioContext
  });
  return starts;
}

function installCaptureContext() {
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
        disconnect: vi.fn(),
      };
    }
    close() {
      return Promise.resolve();
    }
  }
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: FakeAudioContext,
  });
}

beforeEach(() => {
  process.env.VITE_VOCALIZE_API_BASE_URL = "http://example.test";
  delete process.env.VITE_VOCALIZE_WS_BASE_URL;
  localStorage.clear();
  replaceMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: undefined,
  });
});

describe("<LivePageClient>", () => {
  it("renders PreflightChat when phase=collecting (and not TranscriptStream)", async () => {
    const sock = makeFakeSocket();
    const getSession = vi.fn().mockResolvedValue(defaultGetSessionResponse("draft"));
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_url, _id, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession,
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "draft",
      current: "collecting",
    });
    expect(
      await screen.findByLabelText(zh.preflight_chat.aria_label),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(zh.transcript_stream.aria_label),
    ).not.toBeInTheDocument();
  });

  it("renders with default device preferences when localStorage is unavailable", async () => {
    vi.stubGlobal("localStorage", undefined);
    const sock = makeFakeSocket();

    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_url, _id, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );

    await sock.ready();
    expect(
      await screen.findByLabelText(zh.preflight_chat.aria_label),
    ).toBeInTheDocument();
  });

  it("test_text_supplement_input_renders_during_collecting", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("collecting")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    expect(
      await screen.findByPlaceholderText(zh.supplement_input.placeholder_preflight),
    ).toBeInTheDocument();
  });

  it("test_text_supplement_input_renders_during_ready_to_dial", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("ready_to_dial")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    expect(
      await screen.findByPlaceholderText(zh.supplement_input.placeholder_preflight),
    ).toBeInTheDocument();
  });

  it("test_text_supplement_input_not_visible_in_call_phase_below_PreflightChat", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    await screen.findByLabelText(zh.transcript_stream.aria_label);
    expect(
      screen.queryByPlaceholderText(zh.supplement_input.placeholder_preflight),
    ).not.toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(zh.supplement_input.placeholder_default),
    ).toBeInTheDocument();
  });

  it("test_supplement_typed_during_collecting_dispatches_text_input_frame", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("collecting")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    const input = await screen.findByPlaceholderText(
      zh.supplement_input.placeholder_preflight,
    );
    await userEvent.type(input, "改成两个人");
    await userEvent.click(screen.getByRole("button", {
      name: zh.supplement_input.send,
    }));

    expect(sock.fake.sent).toContainEqual({
      type: "text_input",
      text: "改成两个人",
      lang_hint: "zh",
      mode: "default",
    });
  });

  it("test_chip_clears_after_14s_for_filler_keepalive_via_timer", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    await screen.findByLabelText(zh.transcript_stream.aria_label);

    try {
      vi.useFakeTimers();
      act(() => {
        sock.fake.emit({
          type: "transcript_update",
          id: "filler-1",
          role: "ai_to_merchant",
          text: "请稍等",
          lang: "zh",
          is_final: true,
          subtype: "filler",
          parent_id: null,
          segment_id: "seg-1",
          created_at: "x",
        });
      });
      expect(screen.getByText(zh.ai_status.filler_active)).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(14000);
      });
      expect(screen.queryByText(zh.ai_status.filler_active)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders HandoverPanel when phase=ready_to_dial", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "draft",
      current: "ready_to_dial",
    });
    expect(
      await screen.findByText(zh.handover.title),
    ).toBeInTheDocument();
  });

  it("renders TranscriptStream when phase=execution_active", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "ready_to_dial",
      current: "execution_active",
    });
    expect(
      await screen.findByLabelText(zh.transcript_stream.aria_label),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText(zh.preflight_chat.aria_label),
    ).not.toBeInTheDocument();
  });

  it("passes server ai_to_merchant audio into BrowserAudioBridge playback", async () => {
    const starts = installPlaybackContext();
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "ready_to_dial",
      current: "execution_active",
    });
    await screen.findByLabelText(zh.transcript_stream.aria_label);

    sock.fake.emitAudio({ role: "ai_to_merchant", pcm: new Uint8Array(320) });
    sock.fake.emitAudio({ role: "ai_to_merchant", pcm: new Uint8Array(320) });
    sock.fake.emitAudio({ role: "ai_to_merchant", pcm: new Uint8Array(320) });

    await waitFor(() => expect(starts).toHaveLength(3));
    expect(
      (BrowserAudioBridge as unknown as { __test_handle__?: unknown }).__test_handle__,
    ).toBeTruthy();
  });

  it("keeps the supplement input visible during clarification phases", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "execution_active",
      current: "await_user_clarification",
    });
    sock.fake.emit({
      type: "clarification_request",
      field: "allergies",
      question: "有过敏吗？",
      lang: "zh",
      timeout_s: 30,
    });

    expect(
      await screen.findByLabelText(zh.transcript_stream.aria_label),
    ).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText(zh.supplement_input.placeholder_default),
    ).toBeInTheDocument();
  });

  it("test_on_demand_translate_round_trip_dispatches_pending_mark_and_sends_frame", async () => {
    const sock = makeFakeSocket();
    const session = defaultGetSessionResponse("execution_active");
    session.auto_translate_merchant = false;
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(session),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    await screen.findByLabelText(zh.transcript_stream.aria_label);
    sock.fake.emit({
      type: "transcript_update",
      id: "m-1",
      role: "merchant_to_ai",
      text: "We can do 7 pm.",
      lang: "en",
      is_final: true,
      subtype: "original",
      parent_id: null,
      segment_id: null,
      created_at: "x",
    });

    const translateButton = await screen.findByRole("button", {
      name: zh.transcript_stream.translate_button,
    });
    await userEvent.click(translateButton);

    expect(
      sock.fake.sent.find(
        (f) => f.type === "on_demand_translate" && f.transcript_id === "m-1",
      ),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: zh.transcript_stream.translate_button }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("……")).toBeInTheDocument();

    sock.fake.emit({
      type: "transcript_update",
      id: "tr-1",
      role: "ai_to_user",
      text: "可以晚上7点。",
      lang: "zh",
      is_final: true,
      subtype: "translation",
      parent_id: "m-1",
      segment_id: null,
      created_at: "x",
    });

    await waitFor(() =>
      expect(screen.queryByText("……")).not.toBeInTheDocument(),
    );
  });

  it("sends the latest persisted device preferences when settings change", async () => {
    localStorage.setItem("vocalize.device.output_id", "spk-1");
    localStorage.setItem("vocalize.device.aec", "false");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: vi.fn().mockResolvedValue(mockDevices),
      },
    });
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();

    await userEvent.click(screen.getByRole("button", { name: zh.settings.title }));
    await waitFor(() => expect(screen.getByText("USB Microphone")).toBeInTheDocument());
    const selects = screen.getAllByRole("combobox");
    await userEvent.selectOptions(selects[0], "mic-2");

    expect(sock.fake.sent).toContainEqual({
      type: "set_devices",
      input_id: "mic-2",
      output_id: "spk-1",
      aec: false,
    });
  });

  it("passes persisted device preferences into BrowserAudioBridge capture", async () => {
    localStorage.setItem("vocalize.device.input_id", "mic-2");
    localStorage.setItem("vocalize.device.output_id", "spk-1");
    localStorage.setItem("vocalize.device.aec", "false");
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );

    await sock.ready();
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        deviceId: { exact: "mic-2" },
        echoCancellation: false,
        noiseSuppression: true,
        autoGainControl: true,
      },
    }));
  });

  it("disables microphone and AEC settings while active-call capture is switching", async () => {
    localStorage.setItem("vocalize.device.input_id", "mic-1");
    installCaptureContext();
    const switchResolver: {
      current?: (stream: { getTracks: () => never[] }) => void;
    } = {};
    const restoredStream = { getTracks: () => [] };
    const getUserMedia = vi
      .fn()
      .mockResolvedValue(restoredStream)
      .mockResolvedValueOnce({ getTracks: () => [] })
      .mockImplementationOnce(() => new Promise((resolve) => {
        switchResolver.current = resolve;
      }));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: vi.fn().mockResolvedValue(mockDevices),
        getUserMedia,
      },
    });
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );

    await sock.ready();
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: zh.settings.title }));
    await waitFor(() => expect(screen.getByText("USB Microphone")).toBeInTheDocument());

    const selects = screen.getAllByRole("combobox");
    await userEvent.selectOptions(selects[0], "mic-2");

    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
    const checkboxes = screen.getAllByRole("checkbox");
    try {
      expect(selects[0]).toBeDisabled();
      expect(checkboxes[0]).toBeDisabled();
    } finally {
      switchResolver.current?.(restoredStream);
    }
  });

  it("renders PostCallReview when phase=post_call_review", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "execution_active",
      current: "post_call_review",
    });
    // The empty-state PostCallReview shows the empty-state heading. Use a
    // function matcher to allow whitespace/text-node fragmentation around
    // the trailing checkmark glyph.
    expect(
      await screen.findByText((content) =>
        content.includes("通话顺利完成"),
      ),
    ).toBeInTheDocument();
  });

  it("test_live_page_dispatches_connection_state_on_socket_events", async () => {
    let now = 1000;
    vi.spyOn(performance, "now").mockImplementation(() => now);
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "execution_active",
      current: "post_call_review",
    });
    await screen.findByText((content) => content.includes("通话顺利完成"));

    sock.fake.reconnectAttempt();
    expect(await screen.findByText(zh.errors.ws_disconnect)).toBeInTheDocument();

    now = 3501;
    sock.fake.reconnected();
    expect(
      await screen.findByText(zh.session.recovered),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(zh.errors.ws_disconnect)).not.toBeInTheDocument(),
    );
  });

  it("shows backend completion summary on terminal state_update", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "state_update",
      diff: {
        event: "completed",
        phase: "completed",
        summary: "预订成功：五月十号晚上九点，四位。",
      },
    });

    expect(
      await screen.findByText("预订成功：五月十号晚上九点，四位。"),
    ).toBeInTheDocument();
  });

  it("Hangup + UserTakeover buttons visible during execution_active only", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    // Initially in `draft` — no call-phase footer.
    expect(
      screen.queryByRole("button", { name: zh.hangup.button }),
    ).not.toBeInTheDocument();
    sock.fake.emit({
      type: "phase_change",
      previous: "ready_to_dial",
      current: "execution_active",
    });
    expect(
      await screen.findByRole("button", { name: zh.hangup.button }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: new RegExp(`${zh.user_takeover.active}|${zh.user_takeover.inactive}`),
      }),
    ).toBeInTheDocument();
    sock.fake.emit({
      type: "phase_change",
      previous: "execution_active",
      current: "callback_active",
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", {
          name: new RegExp(`${zh.user_takeover.active}|${zh.user_takeover.inactive}`),
        }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.getByPlaceholderText(zh.supplement_input.placeholder_default),
    ).toBeInTheDocument();
  });

  it("PreflightSummaryBanner visible during execution_active and post_call_review", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "phase_change",
      previous: "ready_to_dial",
      current: "execution_active",
    });
    // PreflightSummaryBanner has a toggle button with the slot summary or
    // the "no_summary" placeholder when slots are empty.
    await waitFor(() =>
      expect(
        screen.getAllByText(zh.preflight_summary.no_summary).length,
      ).toBeGreaterThan(0),
    );
    sock.fake.emit({
      type: "phase_change",
      previous: "execution_active",
      current: "post_call_review",
    });
    await waitFor(() =>
      expect(
        screen.getAllByText(zh.preflight_summary.no_summary).length,
      ).toBeGreaterThan(0),
    );
  });

  it("renders ClarificationModal when active_clarification is non-null", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "clarification_request",
      field: "allergies",
      question: "有过敏吗？",
      lang: "zh",
      timeout_s: 30,
    });
    expect(
      await screen.findByText("有过敏吗？", { exact: false }),
    ).toBeInTheDocument();
  });

  it("clicking HandoverPanel takeover sends mode_change(call_listening)", async () => {
    const sock = makeFakeSocket();
    const getTracks = vi.fn(() => [{ stop: vi.fn() }]);
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    // Move to ready_to_dial + readiness passed so the takeover button is enabled.
    sock.fake.emit({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 1,
    });
    sock.fake.emit({
      type: "phase_change",
      previous: "draft",
      current: "ready_to_dial",
    });
    const takeoverBtn = await screen.findByRole("button", {
      name: zh.handover.takeover_button,
    });
    await userEvent.click(takeoverBtn);
    await waitFor(() => expect(getUserMedia).toHaveBeenCalled());
    expect(
      sock.fake.sent.find(
        (f) => f.type === "mode_change" && f.mode === "call_listening",
      ),
    ).toBeTruthy();
  });

  it("does not send handover mode_change when microphone permission fails", async () => {
    const sock = makeFakeSocket();
    const getUserMedia = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 1,
    });
    sock.fake.emit({
      type: "phase_change",
      previous: "draft",
      current: "ready_to_dial",
    });

    await userEvent.click(await screen.findByRole("button", {
      name: zh.handover.takeover_button,
    }));

    await waitFor(() => expect(getUserMedia).toHaveBeenCalled());
    expect(
      sock.fake.sent.find(
        (f) => f.type === "mode_change" && f.mode === "call_listening",
      ),
    ).toBeUndefined();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      zh.handover.mic_error,
    );
  });

  it("keeps the handover sheet open but disabled after readiness backflow", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 1,
    });
    sock.fake.emit({
      type: "phase_change",
      previous: "draft",
      current: "ready_to_dial",
    });
    expect(await screen.findByText(zh.handover.title)).toBeInTheDocument();

    sock.fake.emit({
      type: "readiness_change",
      passed: false,
      missing_critical: ["date"],
      confidence: 0.2,
    });
    sock.fake.emit({
      type: "phase_change",
      previous: "ready_to_dial",
      current: "collecting",
    });

    expect(await screen.findByText(zh.handover.title)).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: zh.handover.takeover_button,
    })).toBeDisabled();
  });

  it("test_cross_lang_notice_uses_relay_hint_when_takeover_active_and_langs_differ", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    await userEvent.click(screen.getByRole("button", {
      name: new RegExp(zh.merchant_lang.label),
    }));
    await userEvent.click(screen.getByLabelText(zh.merchant_lang.en));
    await userEvent.click(screen.getByRole("button", {
      name: zh.merchant_lang.save,
    }));
    await userEvent.click(await screen.findByRole("button", {
      name: zh.user_takeover.inactive,
    }));

    expect(await screen.findByText(
      zh.user_takeover.relay_hint,
    )).toBeInTheDocument();
  });

  it("test_cross_lang_notice_hidden_when_languages_match", async () => {
    const sock = makeFakeSocket();
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "state_update",
      diff: { merchant_lang: "zh" },
    });
    await userEvent.click(await screen.findByRole("button", {
      name: zh.user_takeover.inactive,
    }));

    expect(screen.queryByText(zh.user_takeover.relay_hint)).not.toBeInTheDocument();
  });

  it("language toggle preserves live session socket and conversation-language state", async () => {
    const sock = makeFakeSocket();
    const socketFactory = vi.fn((_u, _i, h) => {
      sock.setHandlers(h);
      return sock.socket;
    });
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={socketFactory}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("execution_active")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    sock.fake.emit({
      type: "state_update",
      diff: { merchant_lang: "en" },
    });
    await userEvent.click(await screen.findByRole("button", {
      name: zh.user_takeover.inactive,
    }));
    expect(await screen.findByText(zh.user_takeover.relay_hint)).toBeInTheDocument();

    sock.fake.sent = [];
    await userEvent.click(screen.getByRole("button", { name: "Switch to English" }));

    expect(replaceMock).toHaveBeenCalledWith(
      "/en/live/s-1?ws=ws%3A%2F%2Fexample.test%2Fws%2Fsessions%2Fs-1",
    );
    expect(localStorage.getItem("preferred_ui_lang")).toBe("en");
    expect(socketFactory).toHaveBeenCalledTimes(1);
    expect(sock.fake.sent).not.toContainEqual(expect.objectContaining({
      type: "set_auto_translate",
    }));
    expect(sock.fake.sent).not.toContainEqual(expect.objectContaining({
      type: "set_devices",
    }));
    expect(sock.fake.sent).not.toContainEqual(expect.objectContaining({
      type: "mode_change",
    }));
    expect(screen.getByText(zh.user_takeover.relay_hint)).toBeInTheDocument();
  });

  it("test_user_takeover_typed_text_dispatches_text_input_user_takeover_frame", async () => {
    const sock = makeFakeSocket();
    const session = defaultGetSessionResponse("execution_active");
    session.default_lang = "en";
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(session),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await sock.ready();
    await userEvent.click(await screen.findByRole("button", {
      name: zh.user_takeover.inactive,
    }));
    const input = await screen.findByPlaceholderText(
      zh.supplement_input.placeholder_takeover,
    );
    await userEvent.type(input, "Hello");
    await userEvent.click(screen.getByRole("button", {
      name: zh.supplement_input.send,
    }));

    expect(sock.fake.sent).toContainEqual({
      type: "text_input",
      text: "Hello",
      mode: "user_takeover",
      lang_hint: "en",
    });
  });

  it("calls getSession once on mount with the session id", async () => {
    const sock = makeFakeSocket();
    const getSession = vi.fn().mockResolvedValue(defaultGetSessionResponse("draft"));
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession,
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
    expect(getSession).toHaveBeenCalledWith("s-1");
  });

  it("closes the socket on unmount", async () => {
    const sock = makeFakeSocket();
    const { unmount } = render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(defaultGetSessionResponse("draft")),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );
    unmount();
    expect(sock.fake.closed).toBe(true);
  });

  it("sends cancel_callback when canceling a pending callback", async () => {
    const sock = makeFakeSocket();
    const session = defaultGetSessionResponse("post_call_review");
    session.pending_callbacks = [{
      id: "cb-1",
      assumption_id: "a-1",
      correction: "6",
      note: null,
      status: "queued",
      created_at: "x",
      started_at: null,
      completed_at: null,
      transcript_segment_id: null,
    }];
    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(session),
            deleteSession: vi.fn().mockResolvedValue(undefined),
          }}
        />,
      ),
    );

    await screen.findByRole("button", { name: /取消/i });
    await userEvent.click(screen.getByRole("button", { name: /取消/i }));

    expect(
      sock.fake.sent.find(
        (f) => f.type === "cancel_callback" && f.callback_id === "cb-1",
      ),
    ).toBeTruthy();
  });

  it("keeps the new WebSocket URL when starting a new call from review", async () => {
    const sock = makeFakeSocket();
    const wsUrl = "ws://127.0.0.1:8000/ws/sessions/s-2";
    const session = defaultGetSessionResponse("post_call_review");
    session.uncertain_assumptions = [{
      id: "a-1",
      slot: "party_size",
      question: "How many?",
      assumed_value: 4,
      source: "user_timeout",
      created_at: "2026-05-15T00:00:00Z",
      status: "pending_review",
      correction: null,
      note: null,
      callback_id: null,
    }];
    const deleteSession = vi.fn().mockResolvedValue(undefined);
    const createSession = vi.fn().mockResolvedValue({
      session_id: "s-2",
      ws_url: wsUrl,
      default_lang: "zh",
      preferred_voice_id: null,
      auto_translate_merchant: true,
    });

    render(
      wrap(
        <LivePageClient
          locale="zh"
          sessionId="s-1"
          socketFactory={(_u, _i, h) => {
            sock.setHandlers(h);
            return sock.socket;
          }}
          apiClient={{
            getSession: vi.fn().mockResolvedValue(session),
            deleteSession,
            createSession,
          }}
        />,
      ),
    );

    await sock.ready();
    await userEvent.click(await screen.findByRole("button", {
      name: zh.post_call_review.start_new_call,
    }));
    await userEvent.click(await screen.findByRole("button", {
      name: zh.post_call_review.start_new_call_confirm_primary,
    }));

    await waitFor(() => {
      expect(createSession).toHaveBeenCalledTimes(1);
      expect(replaceMock).toHaveBeenCalledWith(
        `/zh/live/s-2?ws=${encodeURIComponent(wsUrl)}`,
      );
    });
    expect(deleteSession).toHaveBeenCalledWith("s-1");
  });
});
