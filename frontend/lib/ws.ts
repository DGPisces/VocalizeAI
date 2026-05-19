import type {
  SlotAssumption,
  CallbackEntry,
  CallSegment,
  TranscriptMessageRole,
  TranscriptMessageSubtype,
} from "./state";

// TranscriptMessageSubtype mirrors backend subtype "filler" frames.
// TranscriptMessageSubtype mirrors backend subtype "keepalive" frames.

export type ConversationLang = "zh" | "en";
export type BackendMode =
  | "preflight"
  | "call_listening"
  | "call_speaking"
  | "user_takeover"
  | "ended";
export type AudioRole = "ai_to_user" | "ai_to_merchant";
export type TextInputMode = "default" | "user_takeover";

export type TaskPhaseValue =
  | "draft"
  | "task_planning"
  | "collecting"
  | "ready_to_dial"
  | "execution_active"
  | "needs_clarification"
  | "await_user_clarification"
  | "post_call_review"
  | "callback_active"
  | "completed"
  | "failed";

export type ClientFrame =
  | {
      type: "text_input";
      text: string;
      lang_hint?: ConversationLang;
      mode?: TextInputMode;            // default: "default"
    }
  | { type: "mode_change"; mode: BackendMode }
  | { type: "ack_clarification"; slot_value: string }
  | { type: "hangup" }
  | { type: "set_devices"; input_id: string; output_id: string; aec: boolean }
  | { type: "trigger_callback"; callback_id: string }
  | { type: "cancel_callback"; callback_id: string }
  | { type: "restore_callback"; callback_id: string }
  | {
      type: "confirm_assumption";
      assumption_id: string;
      choice: "correct" | "wrong";
      correction: string | null;
      note?: string | null;
    }
  | { type: "set_auto_translate"; value: boolean }
  | { type: "on_demand_translate"; transcript_id: string };

export type ServerFrame =
  | {
      type: "transcript_update";
      id: string;
      role: TranscriptMessageRole;
      text: string;
      lang: ConversationLang | null;
      is_final: boolean;
      subtype: TranscriptMessageSubtype;
      parent_id: string | null;
      segment_id: string | null;
      created_at: string;            // ISO 8601
    }
  | { type: "state_update"; diff: Record<string, unknown> }
  | {
      type: "readiness_change";
      passed: boolean;
      missing_critical: string[];
      confidence: number;
    }
  | {
      type: "clarification_request";
      field: string;
      question: string;
      lang: ConversationLang;
      timeout_s: number;
    }
  | { type: "mode_ack"; mode: BackendMode }
  | { type: "error"; code: number; message_zh: string; message_en: string }
  | { type: "phase_change"; previous: TaskPhaseValue; current: TaskPhaseValue }
  | { type: "call_segment_added"; segment: CallSegment }
  | {
      type: "segment_interrupted";
      segment_id: string;
      reason: "ws_close" | "user_hangup" | "merchant_impatience";
    }
  | { type: "uncertain_assumption_added"; assumption: SlotAssumption }
  | { type: "pending_callback_added"; callback: CallbackEntry }
  | {
      type: "escalation_warning";
      reason: "merchant_impatience";
      holds_used: number;
      message_zh: string;
      message_en: string;
    };

export type DecodedAudioFrame = {
  role: AudioRole;
  pcm: Uint8Array;
};

export function encodeClientFrame(frame: ClientFrame): string {
  return JSON.stringify(frame);
}

export function parseServerFrame(raw: string): ServerFrame {
  const parsed = JSON.parse(raw) as ServerFrame;
  if (!parsed || typeof parsed !== "object" || typeof parsed.type !== "string") {
    throw new Error("invalid server frame");
  }
  return parsed;
}

export function decodeAudioFrame(payload: ArrayBuffer): DecodedAudioFrame {
  const bytes = new Uint8Array(payload);
  if (bytes.length < 2) {
    throw new Error("audio frame missing payload");
  }
  const roleByte = bytes[0];
  const role =
    roleByte === 85
      ? "ai_to_user"
      : roleByte === 77
        ? "ai_to_merchant"
        : null;
  if (role === null) {
    throw new Error(`unknown audio role byte: ${roleByte}`);
  }
  return { role, pcm: bytes.slice(1) };
}

export function terminalReconnectMessage(sessionId: string): string {
  return `WebSocket disconnected. Session ${sessionId} cannot be resumed if the backend removed it.`;
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "::1" ||
    hostname === "[::1]";
}

function effectivePort(url: URL): string {
  if (url.port) return url.port;
  if (url.protocol === "ws:" || url.protocol === "http:") return "80";
  if (url.protocol === "wss:" || url.protocol === "https:") return "443";
  return "";
}

function hostsMatch(actual: URL, expected: URL): boolean {
  if (actual.host === expected.host) return true;
  return (
    isLoopbackHost(actual.hostname) &&
    isLoopbackHost(expected.hostname) &&
    effectivePort(actual) === effectivePort(expected)
  );
}

export function trustedSessionWsUrl(rawUrl: string, sessionId: string): string {
  const apiBase = process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL;
  if (!apiBase) {
    throw new Error("NEXT_PUBLIC_VOCALIZE_API_BASE_URL is required");
  }
  const apiUrl = new URL(apiBase);
  const configuredWsBase = process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL;
  const expectedBase = configuredWsBase
    ? new URL(configuredWsBase)
    : new URL(`${apiUrl.protocol === "https:" ? "wss:" : "ws:"}//${apiUrl.host}`);
  const wsUrl = new URL(rawUrl);
  const expectedPathPrefix =
    expectedBase.pathname === "/" ? "" : expectedBase.pathname.replace(/\/$/, "");
  const expectedPath = `${expectedPathPrefix}/ws/sessions/${encodeURIComponent(sessionId)}`;
  if (
    (expectedBase.protocol !== "ws:" && expectedBase.protocol !== "wss:") ||
    wsUrl.protocol !== expectedBase.protocol ||
    !hostsMatch(wsUrl, expectedBase) ||
    wsUrl.pathname !== expectedPath ||
    wsUrl.username ||
    wsUrl.password ||
    wsUrl.search ||
    wsUrl.hash
  ) {
    throw new Error("Invalid WebSocket URL");
  }
  return wsUrl.toString();
}

export type SocketHandlers = {
  onFrame: (frame: ServerFrame) => void;
  onAudio: (frame: DecodedAudioFrame) => void;
  onError: (message: string) => void;
  onReconnectAttempt?: () => void;
  onReconnected?: () => void;
};

export class VocalizeSocket {
  private ws: WebSocket | null = null;
  private attemptedReconnect = false;
  private closedByClient = false;
  private pendingFrames: ClientFrame[] = [];

  constructor(
    private readonly url: string,
    private readonly sessionId: string,
    private readonly handlers: SocketHandlers
  ) {}

  connect(): void {
    this.closedByClient = false;
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = "arraybuffer";
    this.ws.onopen = () => {
      const wasReconnect = this.attemptedReconnect;
      const pending = this.pendingFrames;
      this.pendingFrames = [];
      for (const frame of pending) {
        this.ws?.send(encodeClientFrame(frame));
      }
      if (wasReconnect) {
        this.handlers.onReconnected?.();
        this.attemptedReconnect = false;
      }
    };
    this.ws.onmessage = (event) => {
      try {
        if (typeof event.data === "string") {
          this.handlers.onFrame(parseServerFrame(event.data));
        } else {
          this.handlers.onAudio(decodeAudioFrame(event.data));
        }
      } catch (error) {
        console.warn("invalid WS frame", error);
      }
    };
    this.ws.onclose = () => {
      if (this.closedByClient) {
        return;
      }
      if (!this.attemptedReconnect) {
        this.attemptedReconnect = true;
        this.handlers.onReconnectAttempt?.();
        this.connect();
        return;
      }
      this.handlers.onError(terminalReconnectMessage(this.sessionId));
    };
    this.ws.onerror = () => {
      if (this.closedByClient) {
        return;
      }
      this.handlers.onError("WebSocket error");
    };
  }

  send(frame: ClientFrame): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(encodeClientFrame(frame));
    } else if (this.ws?.readyState === WebSocket.CONNECTING) {
      this.pendingFrames.push(frame);
    }
  }

  sendAudio(pcm: Uint8Array): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(pcm);
      return true;
    }
    return false;
  }

  bufferedAmount(): number {
    return this.ws?.bufferedAmount ?? 0;
  }

  close(): void {
    this.closedByClient = true;
    this.pendingFrames = [];
    this.ws?.close();
  }
}
