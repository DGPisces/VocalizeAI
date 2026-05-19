"use client";

import React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AudioLevelMeter } from "./AudioLevelMeter";
import type {
  DecodedAudioFrame,
  ServerFrame,
  VocalizeSocket
} from "../lib/ws";
import {
  calculateRms,
  downsampleTo16k,
  floatToPcm16,
  OUTPUT_SAMPLE_RATE,
  pcm16ToFloat32,
  PREFERRED_CHUNK_MS,
  shouldDegradeCapture
} from "../lib/audio";

// ---------------------------------------------------------------------------
// B3a state machine (new public API).
//
// Spec §4.2 — the bridge is dormant until readiness passes. After AI takeover
// the mic is captured, outbound playback is gated, and `user_takeover` mutes
// outbound AI playback while leaving the mic active so merchant audio still
// reaches the user.
// ---------------------------------------------------------------------------
export type AudioBridgeState =
  | "idle"
  | "handover_ready"
  | "call_listening"
  | "call_speaking"
  | "user_takeover"
  | "ended";

// Legacy status shape (kept until LiveConsole is replaced by the new
// /live/[session] page in Phase G1). The `mode` enum no longer carries the
// removed `preflight_*` values — degraded states now emit `idle` /
// `call_listening` / `call_speaking` directly. Existing tests only assert on
// `degraded: true` (objectContaining), so this narrowing is safe.
export type BridgeStatus = {
  permission: "idle" | "granted" | "denied";
  mode: "idle" | "call_listening" | "call_speaking" | "ended";
  degraded: boolean;
};

type CaptureNodes = {
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  processor: ScriptProcessorNode;
  stream: MediaStream;
};

type PlaybackChunkInput = {
  role: DecodedAudioFrame["role"];
  pcm: Uint8Array;
};

type PlaybackResult = {
  scheduled: boolean;
  queuedSeconds: number;
};

type ReleaseAudioEvidenceWindow = Window & {
  __vocalizeReleaseAudio?: {
    browserSpeaker?: Array<{
      source: string;
      role: DecodedAudioFrame["role"];
      scheduled: boolean;
      queuedSeconds: number;
    }>;
  };
};

type CapturePreferences = {
  inputDeviceId?: string;
  echoCancellation: boolean;
};

type SinkRoutableAudioContext = AudioContext & {
  setSinkId?: (sinkId: string) => Promise<void>;
};

type TestHandle = {
  feedAudio: (frame: PlaybackChunkInput) => PlaybackResult;
  handleFrame: (frame: ServerFrame) => void;
  getState: () => AudioBridgeState;
};

const PLAYBACK_CAPTURE_SUPPRESSION_TAIL_MS = 350;

function releaseStream(stream: MediaStream): void {
  stream.getTracks().forEach((track) => track.stop());
}

function closeCapture(capture: CaptureNodes): void {
  capture.processor.disconnect();
  capture.source.disconnect();
  void capture.context.close();
  releaseStream(capture.stream);
}

// Derive the legacy `BridgeStatus.mode` value from the new state machine.
// Single source of truth — used by both the mirror effect and stopCapture so
// the legacy field can never contradict the new state.
function legacyModeFor(state: AudioBridgeState): BridgeStatus["mode"] {
  if (state === "ended") return "ended";
  if (state === "call_speaking") return "call_speaking";
  if (state === "call_listening" || state === "user_takeover") return "call_listening";
  return "idle"; // idle, handover_ready
}

function buildCaptureConstraints({
  inputDeviceId,
  echoCancellation
}: CapturePreferences): MediaStreamConstraints {
  const audio: MediaTrackConstraints = {
    channelCount: 1,
    echoCancellation,
    noiseSuppression: true,
    autoGainControl: true
  };
  if (inputDeviceId) {
    audio.deviceId = { exact: inputDeviceId };
  }
  return { audio };
}

function normalizeInputDeviceId(inputDeviceId?: string): string | undefined {
  const trimmed = inputDeviceId?.trim();
  return trimmed ? trimmed : undefined;
}

function sameCapturePreferences(
  left: CapturePreferences | null,
  right: CapturePreferences
): boolean {
  return (
    left !== null &&
    left.inputDeviceId === right.inputDeviceId &&
    left.echoCancellation === right.echoCancellation
  );
}

/**
 * Props for `BrowserAudioBridge`.
 *
 * Two API surfaces coexist during the B3a transition:
 *
 * - **New B3a props** (`readinessPassed`, `handover`, `takeoverActive`,
 *   `ended`, `onState`, `socket`) drive the new state machine. The new
 *   `/[locale]/live/[session]` page (Phase G1) uses these.
 *
 * - **Legacy props** (`sendAudio`, `getBufferedAmount`, `onStatusChange`,
 *   `playbackFrame`) are preserved for `LiveConsole.tsx` until Phase G1
 *   replaces it. They are scheduled for removal in the G1 cleanup commit.
 *
 * Mixing both APIs for the same channel (e.g., `sendAudio` + `socket`,
 * or `onStatusChange` + `onState`) is supported but discouraged — legacy
 * wins. A `console.warn` fires once in non-production when mixing is
 * detected.
 */
interface Props {
  // --- New B3a props (preferred when present) -----------------------------
  readinessPassed?: boolean;
  handover?: boolean;
  takeoverActive?: boolean;
  ended?: boolean;
  onState?: (state: AudioBridgeState) => void;
  socket?: VocalizeSocket;
  inputDeviceId?: string;
  outputDeviceId?: string;
  echoCancellation?: boolean;
  onDeviceSwitchingChange?: (switching: boolean) => void;
  onDeviceSwitchSuccess?: () => void;
  onDeviceSwitchError?: (messageKey: string) => void;
  onSpeakerFallback?: () => void;

  // --- Legacy props (still consumed by LiveConsole until G1) --------------
  sendAudio?: (pcm: Uint8Array) => boolean | void;
  getBufferedAmount?: () => number;
  onStatusChange?: (status: BridgeStatus) => void;
  playbackFrame?: DecodedAudioFrame | null;
  playbackFrames?: readonly DecodedAudioFrame[];
  onPlaybackFramesConsumed?: (count: number) => void;
}

const MAX_PLAYBACK_QUEUE_S = 5;

export function BrowserAudioBridge(props: Props) {
  const {
    readinessPassed = false,
    handover = false,
    takeoverActive = false,
    ended = false,
    onState,
    socket,
    inputDeviceId,
    outputDeviceId,
    echoCancellation = true,
    onDeviceSwitchingChange,
    onDeviceSwitchSuccess,
    onDeviceSwitchError,
    onSpeakerFallback,
    sendAudio,
    getBufferedAmount,
    onStatusChange,
    playbackFrame,
    playbackFrames,
    onPlaybackFramesConsumed
  } = props;
  const lifecycleManaged =
    !!socket || readinessPassed || handover || takeoverActive || ended;

  // -------------------------------------------------------------------------
  // New state machine — pure function of props. Used to gate getUserMedia and
  // outbound playback regardless of which API the consumer uses.
  // -------------------------------------------------------------------------
  const [state, setState] = useState<AudioBridgeState>("idle");
  const stateRef = useRef<AudioBridgeState>("idle");

  useEffect(() => {
    let next: AudioBridgeState = "idle";
    if (ended) {
      next = "ended";
    } else if (takeoverActive) {
      next = "user_takeover";
    } else if (handover) {
      next = "call_listening";
    } else if (readinessPassed) {
      next = "handover_ready";
    }
    setState((prev) => (prev === next ? prev : next));
  }, [readinessPassed, handover, takeoverActive, ended]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    onState?.(state);
  }, [state, onState]);

  // Dev-mode warning when both legacy and new APIs are mixed for the same
  // channel. Fires once per channel so it stays out of the way during normal
  // dev. Legacy wins (see Props JSDoc).
  const mixWarnedRef = useRef({ sendAudio: false, status: false });
  useEffect(() => {
    if (process.env.NODE_ENV === "production") return;
    const mixedSendAudio = !!sendAudio && !!socket;
    const mixedStatus = !!onStatusChange && !!onState;
    if (mixedSendAudio && !mixWarnedRef.current.sendAudio) {
      mixWarnedRef.current.sendAudio = true;
      console.warn(
        "[BrowserAudioBridge] Both `sendAudio` (legacy) and `socket` (new) " +
        "props provided. Legacy wins until Phase G1 cleanup. Pick one."
      );
    }
    if (mixedStatus && !mixWarnedRef.current.status) {
      mixWarnedRef.current.status = true;
      console.warn(
        "[BrowserAudioBridge] Both `onStatusChange` (legacy) and `onState` " +
        "(new) props provided. Both fire; observe whichever fits your flow."
      );
    }
  }, [sendAudio, socket, onStatusChange, onState]);

  // -------------------------------------------------------------------------
  // Mic capture — new path triggers automatically on call_listening; legacy
  // path triggers via the Start/Stop buttons (kept until G1 lands).
  // -------------------------------------------------------------------------
  const [legacyStatus, setLegacyStatus] = useState<BridgeStatus>({
    permission: "idle",
    mode: "idle",
    degraded: false
  });
  const [error, setError] = useState<string | null>(null);
  const [level, setLevel] = useState(0);
  const legacyStatusRef = useRef(legacyStatus);
  const onStatusChangeRef = useRef(onStatusChange);
  const captureRef = useRef<CaptureNodes | null>(null);
  const startingRef = useRef(false);
  const switchingRef = useRef(false);
  const switchTokenRef = useRef<symbol | null>(null);
  const capturePreferencesRef = useRef<CapturePreferences | null>(null);
  const lastSuccessfulCapturePreferencesRef = useRef<CapturePreferences | null>(null);
  const pendingCaptureMsRef = useRef(0);
  const captureDegradedRef = useRef(false);
  const playbackContextRef = useRef<AudioContext | null>(null);
  const playbackCursorRef = useRef(0);
  const playbackSinkTargetRef = useRef<string | null>(null);
  const playbackSuppressCaptureUntilMsRef = useRef(0);
  const desiredCapturePreferences = useMemo<CapturePreferences>(() => ({
    inputDeviceId: normalizeInputDeviceId(inputDeviceId),
    echoCancellation
  }), [inputDeviceId, echoCancellation]);

  useEffect(() => {
    onStatusChangeRef.current = onStatusChange;
  }, [onStatusChange]);

  const updateLegacyStatus = useCallback((next: BridgeStatus) => {
    legacyStatusRef.current = next;
    setLegacyStatus(next);
    onStatusChangeRef.current?.(next);
  }, []);

  // Map the new state machine onto a legacy `mode` value when consumers still
  // observe `onStatusChange`. Preserves `permission` and `degraded` fields.
  useEffect(() => {
    if (!onStatusChangeRef.current) {
      return;
    }
    const current = legacyStatusRef.current;
    const derivedMode = legacyModeFor(state);
    if (current.mode !== derivedMode) {
      updateLegacyStatus({ ...current, mode: derivedMode });
    }
  }, [state, updateLegacyStatus]);

  const startCapture = useCallback(async (
    preferences: CapturePreferences = desiredCapturePreferences
  ): Promise<boolean> => {
    if (captureRef.current || startingRef.current) {
      return !!captureRef.current;
    }
    startingRef.current = true;
    setError(null);
    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia(
        buildCaptureConstraints(preferences)
      );
      const AudioContextCtor = window.AudioContext;
      if (!AudioContextCtor) {
        releaseStream(stream);
        stream = null;
        setLevel(0);
        updateLegacyStatus({
          permission: "granted",
          mode: "idle",
          degraded: true
        });
        return false;
      }
      const context = new AudioContextCtor();
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const mono16 = downsampleTo16k(input, context.sampleRate);
        setLevel(calculateRms(mono16));
        if (switchingRef.current) {
          return;
        }
        if (performance.now() < playbackSuppressCaptureUntilMsRef.current) {
          pendingCaptureMsRef.current = 0;
          return;
        }
        const bufferedAmount = getBufferedAmount?.() ?? 0;
        pendingCaptureMsRef.current += PREFERRED_CHUNK_MS;
        if (
          shouldDegradeCapture({
            bufferedAmount,
            pendingMs: pendingCaptureMsRef.current
          })
        ) {
          pendingCaptureMsRef.current = 0;
          captureDegradedRef.current = true;
          updateLegacyStatus({
            permission: "granted",
            mode: legacyStatusRef.current.mode,
            degraded: true
          });
          return;
        }
        // Send via whichever sink the consumer wired up. Prefer the explicit
        // sendAudio prop; fall back to the new `socket` prop when only the
        // new API is in use.
        const sink = sendAudio ?? socket?.sendAudio.bind(socket);
        const sent = sink ? sink(floatToPcm16(mono16)) : false;
        if (sent !== false) {
          pendingCaptureMsRef.current = 0;
        }
        if (sent !== false && captureDegradedRef.current) {
          captureDegradedRef.current = false;
          updateLegacyStatus({
            permission: "granted",
            mode: legacyStatusRef.current.mode,
            degraded: false
          });
        }
      };
      source.connect(processor);
      processor.connect(context.destination);
      captureRef.current = { context, source, processor, stream };
      capturePreferencesRef.current = preferences;
      lastSuccessfulCapturePreferencesRef.current = preferences;
      stream = null;
      setLevel(0.05);
      updateLegacyStatus({
        permission: "granted",
        mode: legacyStatusRef.current.mode,
        degraded: false
      });
      return true;
    } catch {
      if (stream) {
        releaseStream(stream);
      }
      setError("Microphone unavailable");
      updateLegacyStatus({
        permission: "denied",
        mode: "idle",
        degraded: false
      });
      return false;
    } finally {
      startingRef.current = false;
    }
  }, [
    desiredCapturePreferences,
    getBufferedAmount,
    sendAudio,
    socket,
    updateLegacyStatus
  ]);

  const stopCapture = useCallback(() => {
    const capture = captureRef.current;
    if (!capture) {
      return;
    }
    closeCapture(capture);
    captureRef.current = null;
    capturePreferencesRef.current = null;
    pendingCaptureMsRef.current = 0;
    captureDegradedRef.current = false;
    setLevel(0);
    // Compute the legacy `mode` from the actual state machine so the legacy
    // status field cannot contradict `state` (e.g., user clicks Stop while
    // the new path is still in `call_listening`).
    updateLegacyStatus({
      permission: "granted",
      mode: legacyModeFor(stateRef.current),
      degraded: false
    });
  }, [updateLegacyStatus]);

  useEffect(() => {
    const captureActive =
      state === "call_listening" ||
      state === "call_speaking" ||
      state === "user_takeover";
    if (
      !captureActive ||
      !captureRef.current ||
      sameCapturePreferences(capturePreferencesRef.current, desiredCapturePreferences)
    ) {
      return;
    }

    let cancelled = false;
    const previousPreferences =
      lastSuccessfulCapturePreferencesRef.current ?? capturePreferencesRef.current;

    async function switchCapture() {
      const switchToken = Symbol("device-switch");
      switchTokenRef.current = switchToken;
      switchingRef.current = true;
      onDeviceSwitchingChange?.(true);

      try {
        const capture = captureRef.current;
        if (capture) {
          closeCapture(capture);
          captureRef.current = null;
          capturePreferencesRef.current = null;
          pendingCaptureMsRef.current = 0;
          captureDegradedRef.current = false;
        }

        const switched = await startCapture(desiredCapturePreferences);
        if (cancelled) {
          return;
        }
        if (switched) {
          onDeviceSwitchSuccess?.();
          return;
        }

        if (previousPreferences) {
          await startCapture(previousPreferences);
        }
        if (!cancelled) {
          onDeviceSwitchError?.("device_settings.switch_error");
        }
      } finally {
        if (switchTokenRef.current === switchToken) {
          switchTokenRef.current = null;
          switchingRef.current = false;
          onDeviceSwitchingChange?.(false);
        }
      }
    }

    void switchCapture();
    return () => {
      cancelled = true;
    };
  }, [
    desiredCapturePreferences,
    onDeviceSwitchError,
    onDeviceSwitchSuccess,
    onDeviceSwitchingChange,
    startCapture,
    state
  ]);

  // New API: auto-start mic capture on transition to call_listening.
  useEffect(() => {
    if (state === "call_listening" && !captureRef.current) {
      void startCapture();
    }
  }, [state, startCapture]);

  // -------------------------------------------------------------------------
  // Outbound playback (AI -> user). Muted in `user_takeover` per spec §4.2.
  // -------------------------------------------------------------------------
  const routePlaybackOutput = useCallback((context: AudioContext) => {
    const target = outputDeviceId?.trim();
    if (!target) {
      playbackSinkTargetRef.current = null;
      return;
    }
    if (playbackSinkTargetRef.current === target) {
      return;
    }
    playbackSinkTargetRef.current = target;
    const routable = context as SinkRoutableAudioContext;
    if (typeof routable.setSinkId !== "function") {
      onSpeakerFallback?.();
      return;
    }
    void routable.setSinkId(target).catch(() => {
      if (playbackSinkTargetRef.current === target) {
        onSpeakerFallback?.();
      }
    });
  }, [onSpeakerFallback, outputDeviceId]);

  useEffect(() => {
    const context = playbackContextRef.current;
    if (context) {
      routePlaybackOutput(context);
    }
  }, [routePlaybackOutput]);

  const playoutChunk = useCallback(
    (frame: PlaybackChunkInput): PlaybackResult => {
      // Spec §4.2 — outbound AI playback is muted while the user holds the
      // line. Merchant-directed audio still plays because typed takeover
      // passthrough is rendered as ai_to_merchant TTS to the speakerphone.
      if (stateRef.current === "user_takeover" && frame.role === "ai_to_user") {
        return { scheduled: false, queuedSeconds: 0 };
      }
      const AudioContextCtor = window.AudioContext;
      if (!AudioContextCtor) {
        return { scheduled: false, queuedSeconds: 0 };
      }
      const context =
        playbackContextRef.current ??
        new AudioContextCtor({ sampleRate: OUTPUT_SAMPLE_RATE });
      playbackContextRef.current = context;
      routePlaybackOutput(context);
      if (context.state === "suspended") {
        void context.resume().catch(() => undefined);
      }
      const samples = new Float32Array(pcm16ToFloat32(frame.pcm));
      const buffer = context.createBuffer(
        1,
        samples.length,
        OUTPUT_SAMPLE_RATE
      );
      buffer.copyToChannel(samples, 0);
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      const startAt = Math.max(context.currentTime, playbackCursorRef.current);
      source.start(startAt);
      playbackCursorRef.current = startAt + buffer.duration;
      const queuedSeconds = Math.max(
        0,
        playbackCursorRef.current - context.currentTime
      );
      playbackSuppressCaptureUntilMsRef.current = Math.max(
        playbackSuppressCaptureUntilMsRef.current,
        performance.now() +
          queuedSeconds * 1000 +
          PLAYBACK_CAPTURE_SUPPRESSION_TAIL_MS
      );
      const result = { scheduled: true, queuedSeconds };
      if (process.env.NEXT_PUBLIC_E2E_AUDIO_HOOK === "1") {
        const evidenceWindow = window as ReleaseAudioEvidenceWindow;
        evidenceWindow.__vocalizeReleaseAudio?.browserSpeaker?.push({
          source: "BrowserAudioBridge",
          role: frame.role,
          scheduled: result.scheduled,
          queuedSeconds: result.queuedSeconds
        });
      }
      return result;
    },
    [routePlaybackOutput]
  );

  // Legacy playback path — driven by `playbackFrame` prop. Retains the
  // degraded-status emission from the original implementation so existing
  // tests continue to pass.
  useEffect(() => {
    if (!playbackFrame) {
      return;
    }
    const result = playoutChunk(playbackFrame);
    if (!result.scheduled) {
      return;
    }
    const current = legacyStatusRef.current;
    updateLegacyStatus({
      permission: current.permission,
      mode: current.mode,
      degraded: current.degraded || result.queuedSeconds > MAX_PLAYBACK_QUEUE_S
    });
  }, [playbackFrame, playoutChunk, updateLegacyStatus]);

  useEffect(() => {
    if (!playbackFrames || playbackFrames.length === 0) {
      return;
    }
    let consumed = 0;
    let degraded = false;
    for (const frame of playbackFrames) {
      const result = playoutChunk(frame);
      consumed += 1;
      degraded = degraded || (
        result.scheduled && result.queuedSeconds > MAX_PLAYBACK_QUEUE_S
      );
    }
    if (degraded) {
      const current = legacyStatusRef.current;
      updateLegacyStatus({
        permission: current.permission,
        mode: current.mode,
        degraded: true
      });
    }
    onPlaybackFramesConsumed?.(consumed);
  }, [
    playbackFrames,
    playoutChunk,
    updateLegacyStatus,
    onPlaybackFramesConsumed
  ]);

  // -------------------------------------------------------------------------
  // mode_ack handler (B3). Backend confirms a transition; the bridge follows
  // unless the user has already taken over (in which case takeover wins).
  // user_takeover and ended come in via props (`takeoverActive`, `ended`).
  // -------------------------------------------------------------------------
  const handleFrame = useCallback((frame: ServerFrame) => {
    if (frame.type !== "mode_ack") {
      return;
    }
    const m = frame.mode;
    if (m === "call_speaking") {
      setState((prev) =>
        prev === "user_takeover" || prev === "ended" ? prev : "call_speaking"
      );
    } else if (m === "call_listening") {
      setState((prev) =>
        prev === "user_takeover" || prev === "ended" ? prev : "call_listening"
      );
    }
  }, []);

  // -------------------------------------------------------------------------
  // Test affordance — only attached in test mode. feedAudio drives the
  // playback path so B2 can assert mute behaviour; handleFrame drives the
  // mode_ack handler so B3 can assert backend-driven transitions.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (process.env.NODE_ENV !== "test") {
      return;
    }
    const handle: TestHandle = {
      feedAudio: (frame) => playoutChunk(frame),
      handleFrame,
      getState: () => stateRef.current
    };
    (
      BrowserAudioBridge as unknown as { __test_handle__?: TestHandle }
    ).__test_handle__ = handle;
    return () => {
      const ref = BrowserAudioBridge as unknown as {
        __test_handle__?: TestHandle;
      };
      if (ref.__test_handle__ === handle) {
        delete ref.__test_handle__;
      }
    };
  }, [playoutChunk, handleFrame]);

  // -------------------------------------------------------------------------
  // Cleanup.
  // -------------------------------------------------------------------------
  useEffect(() => {
    return () => {
      const capture = captureRef.current;
      if (capture) {
        closeCapture(capture);
        captureRef.current = null;
      }
      const playbackContext = playbackContextRef.current;
      if (typeof playbackContext?.close === "function") {
        void playbackContext.close();
      }
      playbackContextRef.current = null;
    };
  }, []);

  // Auto-stop capture whenever the managed B3a lifecycle leaves active call
  // phases. Legacy manual Start/Stop remains manual when no lifecycle props are
  // wired.
  useEffect(() => {
    const captureAllowed =
      state === "call_listening" ||
      state === "call_speaking" ||
      state === "user_takeover";
    if (lifecycleManaged && !captureAllowed && captureRef.current) {
      stopCapture();
    }
  }, [lifecycleManaged, state, stopCapture]);

  return (
    <section className="card stack" aria-label="Audio bridge">
      <div>
        <div className="card-title">Audio</div>
        <p>{legacyStatus.permission === "granted" ? "Connected" : "Not connected"}</p>
      </div>
      {error ? (
        <div className="alert alert--bad" role="alert">
          {error}
        </div>
      ) : null}
      <AudioLevelMeter level={level} label="Microphone level" />
      <button className="btn-secondary" type="button" onClick={() => { void startCapture(); }}>
        Start audio
      </button>
      <button className="btn-secondary" type="button" onClick={stopCapture}>
        Stop audio
      </button>
    </section>
  );
}
