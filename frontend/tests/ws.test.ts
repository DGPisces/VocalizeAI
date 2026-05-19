import { describe, expect, it, vi } from "vitest";
import { MockWebSocket } from "./setup";
import {
  decodeAudioFrame,
  encodeClientFrame,
  parseServerFrame,
  terminalReconnectMessage,
  trustedSessionWsUrl,
  VocalizeSocket
} from "../lib/ws";

describe("ws frame codec", () => {
  it("encodes text_input frames", () => {
    expect(encodeClientFrame({ type: "text_input", text: "hi", lang_hint: "en" })).toBe(
      JSON.stringify({ type: "text_input", text: "hi", lang_hint: "en" })
    );
  });

  it("encodes set_devices frames with AEC", () => {
    expect(
      encodeClientFrame({
        type: "set_devices",
        input_id: "mic-1",
        output_id: "spk-1",
        aec: false,
      })
    ).toBe(JSON.stringify({
      type: "set_devices",
      input_id: "mic-1",
      output_id: "spk-1",
      aec: false,
    }));
  });

  it("decodes role-prefixed outbound audio", () => {
    const payload = new Uint8Array([85, 1, 2, 3]).buffer;
    expect(decodeAudioFrame(payload)).toEqual({
      role: "ai_to_user",
      pcm: new Uint8Array([1, 2, 3])
    });
  });

  it("throws on unknown audio role", () => {
    const payload = new Uint8Array([88, 1]).buffer;
    expect(() => decodeAudioFrame(payload)).toThrow("unknown audio role byte");
  });

  it("parses readiness frames", () => {
    expect(parseServerFrame(JSON.stringify({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 0.9
    }))).toEqual({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 0.9
    });
  });

  it("documents terminal reconnect behavior", () => {
    expect(terminalReconnectMessage("abc")).toContain("abc");
  });

  it("accepts a separately configured backend WebSocket base URL", () => {
    process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "https://api.example.test";
    process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL = "wss://ws.example.test";

    try {
      expect(
        trustedSessionWsUrl("wss://ws.example.test/ws/sessions/abc", "abc")
      ).toBe("wss://ws.example.test/ws/sessions/abc");
    } finally {
      delete process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL;
      delete process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL;
    }
  });

  it("accepts local loopback aliases for the same WebSocket listener", () => {
    process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "http://127.0.0.1:8000";
    delete process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL;

    try {
      expect(
        trustedSessionWsUrl("ws://localhost:8000/ws/sessions/abc", "abc")
      ).toBe("ws://localhost:8000/ws/sessions/abc");
      expect(
        trustedSessionWsUrl("ws://[::1]:8000/ws/sessions/abc", "abc")
      ).toBe("ws://[::1]:8000/ws/sessions/abc");
    } finally {
      delete process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL;
    }
  });

  it("rejects non-loopback host mismatches", () => {
    process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "http://127.0.0.1:8000";
    delete process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL;

    try {
      expect(() =>
        trustedSessionWsUrl("ws://example.test:8000/ws/sessions/abc", "abc")
      ).toThrow("Invalid WebSocket URL");
    } finally {
      delete process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL;
    }
  });
});

describe("VocalizeSocket", () => {
  it("sends JSON and binary frames only while the socket is open", () => {
    const socket = new VocalizeSocket("ws://example.test/ws", "abc", {
      onFrame: vi.fn(),
      onAudio: vi.fn(),
      onError: vi.fn()
    });

    socket.connect();
    const ws = MockWebSocket.instances[0];
    socket.send({ type: "text_input", text: "hi", lang_hint: "en" });
    socket.sendAudio(new Uint8Array([1, 2]));

    expect(ws.sent).toEqual([
      JSON.stringify({ type: "text_input", text: "hi", lang_hint: "en" }),
      new Uint8Array([1, 2])
    ]);

    ws.readyState = MockWebSocket.CLOSED;
    socket.send({ type: "hangup" });
    socket.sendAudio(new Uint8Array([3]));

    expect(ws.sent).toHaveLength(2);
  });

  it("queues JSON frames while the socket is connecting", () => {
    const socket = new VocalizeSocket("ws://example.test/ws", "abc", {
      onFrame: vi.fn(),
      onAudio: vi.fn(),
      onError: vi.fn()
    });

    socket.connect();
    const ws = MockWebSocket.instances[0];
    ws.readyState = MockWebSocket.CONNECTING;

    socket.send({ type: "text_input", text: "early", lang_hint: "en" });
    expect(ws.sent).toEqual([]);

    ws.readyState = MockWebSocket.OPEN;
    ws.onopen?.(new Event("open"));

    expect(ws.sent).toEqual([
      JSON.stringify({ type: "text_input", text: "early", lang_hint: "en" })
    ]);
  });

  it("tries one reconnect probe, then reports a terminal error", () => {
    const onError = vi.fn();
    const socket = new VocalizeSocket("ws://example.test/ws", "abc", {
      onFrame: vi.fn(),
      onAudio: vi.fn(),
      onError
    });

    socket.connect();
    MockWebSocket.instances[0].emitClose();
    expect(MockWebSocket.instances).toHaveLength(2);

    MockWebSocket.instances[1].emitClose();
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("abc"));
  });

  it("test_vocalize_socket_fires_onReconnectAttempt_then_onReconnected", async () => {
    const onReconnectAttempt = vi.fn();
    const onReconnected = vi.fn();
    const socket = new VocalizeSocket("ws://example.test/ws", "abc", {
      onFrame: vi.fn(),
      onAudio: vi.fn(),
      onError: vi.fn(),
      onReconnectAttempt,
      onReconnected,
    });

    socket.connect();
    MockWebSocket.instances[0].emitClose();
    expect(onReconnectAttempt).toHaveBeenCalledTimes(1);
    await Promise.resolve();

    expect(MockWebSocket.instances).toHaveLength(2);
    expect(onReconnected).toHaveBeenCalledTimes(1);
  });

  it("does not reconnect after explicit close", () => {
    const socket = new VocalizeSocket("ws://example.test/ws", "abc", {
      onFrame: vi.fn(),
      onAudio: vi.fn(),
      onError: vi.fn()
    });

    socket.connect();
    socket.close();

    expect(MockWebSocket.instances).toHaveLength(1);
  });
});

describe("B3a server frames", () => {
  it("parses extended transcript_update with id + subtype + parent_id", () => {
    const raw = JSON.stringify({
      type: "transcript_update",
      id: "t-1",
      role: "merchant_to_ai",
      text: "Hello",
      lang: "en",
      is_final: true,
      subtype: "original",
      parent_id: null,
      segment_id: null,
      created_at: "2026-05-07T12:00:00Z",
    });
    const frame = parseServerFrame(raw);
    expect(frame.type).toBe("transcript_update");
    if (frame.type === "transcript_update") {
      expect(frame.id).toBe("t-1");
      expect(frame.role).toBe("merchant_to_ai");
      expect(frame.subtype).toBe("original");
      expect(frame.parent_id).toBeNull();
    }
  });

  it("parses transcript_update with subtype=translation linked to parent", () => {
    const raw = JSON.stringify({
      type: "transcript_update",
      id: "t-2",
      role: "ai_to_user",
      text: "你好",
      lang: "zh",
      is_final: true,
      subtype: "translation",
      parent_id: "t-1",
      segment_id: null,
      created_at: "2026-05-07T12:00:01Z",
    });
    const frame = parseServerFrame(raw);
    if (frame.type === "transcript_update") {
      expect(frame.subtype).toBe("translation");
      expect(frame.parent_id).toBe("t-1");
    }
  });

  it("parses phase_change", () => {
    const f = parseServerFrame(JSON.stringify({
      type: "phase_change",
      previous: "execution_active",
      current: "post_call_review",
    }));
    expect(f.type).toBe("phase_change");
    if (f.type === "phase_change") {
      expect(f.previous).toBe("execution_active");
      expect(f.current).toBe("post_call_review");
    }
  });

  it("parses uncertain_assumption_added", () => {
    const f = parseServerFrame(JSON.stringify({
      type: "uncertain_assumption_added",
      assumption: {
        id: "a-1",
        slot: "party_size",
        question: "how many?",
        assumed_value: 4,
        source: "user_timeout",
        created_at: "2026-05-07T12:00:00Z",
        status: "pending_review",
        correction: null,
        note: null,
        callback_id: null,
      },
    }));
    expect(f.type).toBe("uncertain_assumption_added");
    if (f.type === "uncertain_assumption_added") {
      expect(f.assumption.id).toBe("a-1");
      expect(f.assumption.source).toBe("user_timeout");
    }
  });

  it("parses pending_callback_added", () => {
    const f = parseServerFrame(JSON.stringify({
      type: "pending_callback_added",
      callback: {
        id: "cb-1",
        assumption_id: "a-1",
        correction: "6",
        note: null,
        status: "queued",
        created_at: "2026-05-07T12:00:00Z",
        started_at: null,
        completed_at: null,
        transcript_segment_id: null,
      },
    }));
    expect(f.type).toBe("pending_callback_added");
    if (f.type === "pending_callback_added") {
      expect(f.callback.id).toBe("cb-1");
      expect(f.callback.status).toBe("queued");
    }
  });

  it("parses escalation_warning", () => {
    const f = parseServerFrame(JSON.stringify({
      type: "escalation_warning",
      reason: "merchant_impatience",
      holds_used: 2,
      message_zh: "商家催了三次",
      message_en: "Merchant interrupted 3 times",
    }));
    expect(f.type).toBe("escalation_warning");
    if (f.type === "escalation_warning") {
      expect(f.reason).toBe("merchant_impatience");
      expect(f.holds_used).toBe(2);
    }
  });
});

describe("B3a client frames", () => {
  it("encodes text_input with mode=user_takeover", () => {
    const out = encodeClientFrame({
      type: "text_input",
      text: "yes please",
      lang_hint: "en",
      mode: "user_takeover",
    });
    expect(JSON.parse(out)).toEqual({
      type: "text_input",
      text: "yes please",
      lang_hint: "en",
      mode: "user_takeover",
    });
  });

  it("encodes trigger_callback", () => {
    const out = encodeClientFrame({ type: "trigger_callback", callback_id: "cb-1" });
    expect(JSON.parse(out)).toEqual({ type: "trigger_callback", callback_id: "cb-1" });
  });

  it("encodes cancel_callback", () => {
    const out = encodeClientFrame({ type: "cancel_callback", callback_id: "cb-1" });
    expect(JSON.parse(out)).toEqual({ type: "cancel_callback", callback_id: "cb-1" });
  });

  it("encodes confirm_assumption with note", () => {
    const out = encodeClientFrame({
      type: "confirm_assumption",
      assumption_id: "a-1",
      choice: "wrong",
      correction: "6",
      note: "actually six adults",
    });
    expect(JSON.parse(out)).toEqual({
      type: "confirm_assumption",
      assumption_id: "a-1",
      choice: "wrong",
      correction: "6",
      note: "actually six adults",
    });
  });

  it("encodes set_auto_translate", () => {
    const out = encodeClientFrame({ type: "set_auto_translate", value: false });
    expect(JSON.parse(out)).toEqual({ type: "set_auto_translate", value: false });
  });

  it("encodes on_demand_translate", () => {
    const out = encodeClientFrame({ type: "on_demand_translate", transcript_id: "t-42" });
    expect(JSON.parse(out)).toEqual({ type: "on_demand_translate", transcript_id: "t-42" });
  });
});
