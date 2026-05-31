import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "@/src/router";
import { useTranslations } from "@/src/i18n";
import {
  Activity,
  CheckCircle2,
  PhoneCall,
  Settings2,
  Stethoscope,
} from "lucide-react";

import {
  applyServerFrame,
  useSessionStore,
} from "../../../../lib/state";
import {
  trustedSessionWsUrl,
  VocalizeSocket,
  type ClientFrame,
  type DecodedAudioFrame,
  type SocketHandlers,
} from "../../../../lib/ws";
import {
  createSession as defaultCreateSession,
  deleteSession as defaultDeleteSession,
  getSession as defaultGetSession,
  type GetSessionResponse,
  type ReviewCallSegment,
  type SessionResponse,
} from "../../../../lib/api";

import { PreflightChat } from "../../../../components/PreflightChat";
import { TranscriptStream } from "../../../../components/TranscriptStream";
import { PostCallReview } from "../../../../components/PostCallReview";
import { HandoverPanel } from "../../../../components/HandoverPanel";
import { ClarificationModal } from "../../../../components/ClarificationModal";
import { HangupButton } from "../../../../components/HangupButton";
import { UserTakeoverButton } from "../../../../components/UserTakeoverButton";
import { ReadinessIndicator } from "../../../../components/ReadinessIndicator";
import { ConnectionStateChip } from "../../../../components/ConnectionStateChip";
import { SessionRecoveredToast } from "../../../../components/SessionRecoveredToast";
import { MerchantLangBadge } from "../../../../components/MerchantLangBadge";
import { PreflightSummaryBanner } from "../../../../components/PreflightSummaryBanner";
import { Settings } from "../../../../components/Settings";
import type {
  DevicePreferences,
  DeviceSwitchStatus,
} from "../../../../components/DeviceSettings";
import { LanguageToggle } from "../../../../components/LanguageToggle";
import { BrowserAudioBridge } from "../../../../components/BrowserAudioBridge";
import { TextSupplementInput } from "../../../../components/TextSupplementInput";

// ---------------------------------------------------------------------------
// Socket abstraction. The runtime uses VocalizeSocket; tests inject a fake
// matching this shape.
// ---------------------------------------------------------------------------
export interface SocketLike {
  connect(): void;
  close(): void;
  send(frame: ClientFrame): void;
  sendAudio(pcm: Uint8Array): boolean;
  bufferedAmount(): number;
}

export type SocketFactory = (
  url: string,
  sessionId: string,
  handlers: SocketHandlers,
) => SocketLike;

export interface ApiClient {
  getSession: (sessionId: string) => Promise<GetSessionResponse>;
  deleteSession: (sessionId: string) => Promise<void>;
  createSession?: () => Promise<SessionResponse>;
}

interface Props {
  locale: string;
  sessionId: string;
  initialWsUrl?: string;
  debug?: boolean;
  // Optional injection points for tests. Production paths fall back to
  // defaults (`new VocalizeSocket(...)`, `getSession`, `deleteSession`).
  socketFactory?: SocketFactory;
  apiClient?: ApiClient;
}

const DEFAULT_SOCKET_FACTORY: SocketFactory = (url, sessionId, handlers) =>
  new VocalizeSocket(url, sessionId, handlers);

const DEFAULT_API_CLIENT: ApiClient = {
  getSession: defaultGetSession,
  deleteSession: defaultDeleteSession,
  createSession: defaultCreateSession,
};

const MERCHANT_MIC_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

const DEVICE_STORAGE_KEYS = {
  inputId: "vocalize.device.input_id",
  outputId: "vocalize.device.output_id",
  aec: "vocalize.device.aec",
} as const;

const DEFAULT_DEVICE_PREFERENCES: DevicePreferences = {
  inputId: "",
  outputId: "",
  aec: true,
};

function readDevicePreferences(): DevicePreferences {
  if (typeof window === "undefined") {
    return DEFAULT_DEVICE_PREFERENCES;
  }
  try {
    return {
      inputId: localStorage.getItem(DEVICE_STORAGE_KEYS.inputId) ?? "",
      outputId: localStorage.getItem(DEVICE_STORAGE_KEYS.outputId) ?? "",
      aec: localStorage.getItem(DEVICE_STORAGE_KEYS.aec) !== "false",
    };
  } catch {
    return DEFAULT_DEVICE_PREFERENCES;
  }
}

function deriveReviewStatus(
  callSegments: ReviewCallSegment[],
): "completed" | "interrupted" | "escalated" {
  const last = callSegments[callSegments.length - 1];
  if (last?.interrupt_reason === "merchant_impatience") {
    return "escalated";
  }
  if (last?.interrupt_reason === "ws_close" || last?.interrupt_reason === "user_hangup") {
    return "interrupted";
  }
  return "completed";
}

async function verifyMerchantMicAvailable(): Promise<void> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("getUserMedia unavailable");
  }
  const stream = await navigator.mediaDevices.getUserMedia(
    MERCHANT_MIC_CONSTRAINTS,
  );
  stream.getTracks().forEach((track) => track.stop());
}

export function LivePageClient({
  locale,
  sessionId,
  initialWsUrl,
  debug = false,
  socketFactory,
  apiClient,
}: Props) {
  const t = useTranslations();
  const router = useRouter();
  const searchParams = useSearchParams();
  const wsUrlFromQuery = initialWsUrl ?? searchParams?.get("ws") ?? "";

  const factory = socketFactory ?? DEFAULT_SOCKET_FACTORY;
  const api = apiClient ?? DEFAULT_API_CLIENT;

  const { state, dispatch } = useSessionStore();
  const [error, setError] = useState<string | null>(null);
  const [audioFrames, setAudioFrames] = useState<DecodedAudioFrame[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [handoverSheetOpen, setHandoverSheetOpen] = useState(false);
  const [showRecoveredToast, setShowRecoveredToast] = useState(false);
  const [devicePreferences, setDevicePreferences] =
    useState<DevicePreferences>(DEFAULT_DEVICE_PREFERENCES);
  const [deviceSwitchingStatus, setDeviceSwitchingStatus] =
    useState<DeviceSwitchStatus | null>(null);
  const [micPreparing, setMicPreparing] = useState(false);
  const [micError, setMicError] = useState<string | null>(null);
  const [merchantLang, setMerchantLang] = useState<"zh" | "en" | "auto">(
    "auto",
  );

  // Strict-Mode safe socket lifecycle. We hold the socket in a ref + boot
  // guard so the WebSocket opens at most once per mount.
  const socketRef = useRef<SocketLike | null>(null);
  const bootedRef = useRef(false);
  const lastDisconnectAt = useRef<number | null>(null);
  const phaseRef = useRef(state.phase);

  useEffect(() => {
    phaseRef.current = state.phase;
  }, [state.phase]);

  useEffect(() => {
    setDevicePreferences(readDevicePreferences());
  }, []);

  // Hydrate state slices once on mount via REST.
  useEffect(() => {
    let cancelled = false;
    api
      .getSession(sessionId)
      .then((s) => {
        if (cancelled) return;
        dispatch({
          type: "hydrate",
          partial: {
            phase: s.phase,
            uncertain_assumptions: s.uncertain_assumptions,
            pending_callbacks: s.pending_callbacks,
            auto_translate_merchant: s.auto_translate_merchant,
            task_description: s.task_description,
            user_lang: s.default_lang,
          },
        });
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
    // We hydrate exactly once on mount per spec.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Re-hydrate when phase transitions to post_call_review so we get the
  // latest assumption + callback lists from the backend.
  useEffect(() => {
    if (state.phase !== "post_call_review") return;
    let cancelled = false;
    api
      .getSession(sessionId)
      .then((s) => {
        if (cancelled) return;
        dispatch({
          type: "hydrate",
          partial: {
            uncertain_assumptions: s.uncertain_assumptions,
            pending_callbacks: s.pending_callbacks,
          },
        });
      })
      .catch(() => {
        // Non-fatal — the modal already shows whatever we have buffered.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.phase, sessionId]);

  // Open the WS once on mount.
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    if (!wsUrlFromQuery) {
      // No URL provided — surface the error but stay mounted so other
      // bits of the page (e.g., copy, settings) still render.
      setError("Missing WebSocket URL");
      return;
    }
    let trustedUrl: string;
    try {
      // Trust check is bypassed for fake sockets in tests since the test
      // factory ignores the URL; in production we still validate.
      trustedUrl = socketFactory
        ? wsUrlFromQuery
        : trustedSessionWsUrl(wsUrlFromQuery, sessionId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    const handlers: SocketHandlers = {
      onFrame: (frame) => applyServerFrame(dispatch, frame),
      onAudio: (frame) => setAudioFrames((prev) => [...prev, frame]),
      onError: (m) => setError(m),
      onReconnectAttempt: () => {
        lastDisconnectAt.current = performance.now();
        dispatch({ type: "connection_state_changed", state: "reconnecting" });
      },
      onReconnected: () => {
        const disconnectedFor =
          performance.now() - (lastDisconnectAt.current ?? performance.now());
        dispatch({ type: "connection_state_changed", state: "connected" });
        if (disconnectedFor > 2000 && phaseRef.current === "post_call_review") {
          setShowRecoveredToast(true);
        }
      },
    };
    const socket = factory(trustedUrl, sessionId, handlers);
    socket.connect();
    socketRef.current = socket;
    return () => {
      bootedRef.current = false;
      socket.close();
      socketRef.current = null;
    };
    // The factory closure captures dispatch, which is stable across renders.
    // We intentionally run this effect once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------------------------------------
  // Outgoing frame helpers — wrap socketRef so callers don't need to deal
  // with the null check.
  // -------------------------------------------------------------------------
  const sendFrame = useCallback((frame: ClientFrame) => {
    socketRef.current?.send(frame);
  }, []);

  const onPlaybackFramesConsumed = useCallback((count: number) => {
    setAudioFrames((prev) => prev.slice(count));
  }, []);

  const sendText = useCallback(
    (p: { text: string; lang_hint?: "zh" | "en"; mode: "default" | "user_takeover" }) => {
      sendFrame({
        type: "text_input",
        text: p.text,
        lang_hint: p.lang_hint,
        mode: p.mode,
      });
      // Echo into preflight chat history when user types during preflight.
      if (p.mode === "default" && isPreflight(state.phase)) {
        dispatch({
          type: "preflight_local_input_appended",
          entry: {
            id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            text: p.text,
            ts: new Date().toISOString(),
          },
        });
      }
    },
    [sendFrame, state.phase, dispatch],
  );

  const onTakeover = useCallback(async () => {
    if (micPreparing) return;
    setMicPreparing(true);
    setMicError(null);
    try {
      await verifyMerchantMicAvailable();
      sendFrame({ type: "mode_change", mode: "call_listening" });
    } catch {
      setMicError(t("handover.mic_error"));
    } finally {
      setMicPreparing(false);
    }
  }, [sendFrame, micPreparing, t]);

  const onUserTakeoverToggle = useCallback(
    (next: boolean) => {
      dispatch({ type: "user_takeover_toggle", active: next });
      sendFrame({
        type: "mode_change",
        mode: next ? "user_takeover" : "call_listening",
      });
    },
    [sendFrame, dispatch],
  );

  const onHangup = useCallback(() => {
    sendFrame({ type: "hangup" });
  }, [sendFrame]);

  const onClarificationAck = useCallback(
    (slot_value: string) => {
      sendFrame({ type: "ack_clarification", slot_value });
      dispatch({ type: "clarification_close" });
    },
    [sendFrame, dispatch],
  );

  const onClarificationTimeout = useCallback(() => {
    dispatch({ type: "clarification_close" });
  }, [dispatch]);

  const onConfirm = useCallback(
    (assumption_id: string) => {
      sendFrame({
        type: "confirm_assumption",
        assumption_id,
        choice: "correct",
        correction: null,
      });
    },
    [sendFrame],
  );

  const onCorrect = useCallback(
    (p: { assumption_id: string; correction: string; note: string | null }) => {
      sendFrame({
        type: "confirm_assumption",
        assumption_id: p.assumption_id,
        choice: "wrong",
        correction: p.correction,
        note: p.note,
      });
    },
    [sendFrame],
  );

  const onTriggerCallback = useCallback(
    (callback_id: string) => {
      sendFrame({ type: "trigger_callback", callback_id });
    },
    [sendFrame],
  );

  const onCancelCallback = useCallback(
    (callback_id: string) => {
      dispatch({ type: "cancel_pending_callback", callback_id });
      sendFrame({ type: "cancel_callback", callback_id });
    },
    [dispatch, sendFrame],
  );

  const onRestoreCallback = useCallback(
    (callback_id: string) => {
      dispatch({ type: "restore_pending_callback", callback_id });
      sendFrame({ type: "restore_callback", callback_id });
    },
    [dispatch, sendFrame],
  );

  const onAutoTranslateChange = useCallback(
    (next: boolean) => {
      try {
        localStorage.setItem("auto_translate_merchant", String(next));
      } catch {
        /* ignore */
      }
      dispatch({ type: "set_auto_translate", value: next });
      sendFrame({ type: "set_auto_translate", value: next });
    },
    [sendFrame, dispatch],
  );

  const onDevicePreferencesChange = useCallback(
    (preferences: DevicePreferences) => {
      setDevicePreferences(preferences);
      sendFrame({
        type: "set_devices",
        input_id: preferences.inputId,
        output_id: preferences.outputId,
        aec: preferences.aec,
      });
    },
    [sendFrame],
  );

  const onDeviceSwitchingChange = useCallback((switching: boolean) => {
    if (switching) {
      setDeviceSwitchingStatus({ kind: "switching" });
    }
  }, []);

  const onDeviceSwitchSuccess = useCallback(() => {
    setDeviceSwitchingStatus({ kind: "success" });
  }, []);

  const onDeviceSwitchError = useCallback((messageKey: string) => {
    setDeviceSwitchingStatus(
      messageKey === "device_settings.switch_error"
        ? { kind: "switch_error" }
        : null,
    );
  }, []);

  const onSpeakerFallback = useCallback(() => {
    setDeviceSwitchingStatus({ kind: "speaker_fallback" });
  }, []);

  const onDemandTranslate = useCallback(
    (transcript_id: string) => {
      dispatch({ type: "translation_pending_mark", id: transcript_id });
      sendFrame({ type: "on_demand_translate", transcript_id });
    },
    [dispatch, sendFrame],
  );

  const onDismiss = useCallback(async () => {
    try {
      await api.deleteSession(sessionId);
    } catch {
      // Backend may already be gone; proceed regardless.
    }
    sendFrame({ type: "mode_change", mode: "ended" });
    router.replace(`/${locale}/`);
  }, [api, sessionId, sendFrame, router, locale]);

  const onStartNewCall = useCallback(async () => {
    try {
      await api.deleteSession(sessionId);
    } catch {
      // Backend may already be gone; creating a fresh session still works.
    }
    const next = await (api.createSession ?? defaultCreateSession)();
    router.push(
      `/${locale}/live/${next.session_id}?ws=${encodeURIComponent(next.ws_url)}`,
    );
  }, [api, sessionId, router, locale]);

  // -------------------------------------------------------------------------
  // Render decisions — phase drives the main slot.
  // -------------------------------------------------------------------------
  const isClarificationPhase =
    state.phase === "needs_clarification" ||
    state.phase === "await_user_clarification";
  const isCallPhase =
    state.phase === "execution_active" ||
    state.phase === "callback_active" ||
    isClarificationPhase;
  const takeoverControlsEnabled = state.phase === "execution_active";
  const showSummaryBanner = isCallPhase || state.phase === "post_call_review";
  const showHandover =
    handoverSheetOpen &&
    (state.phase === "ready_to_dial" || state.phase === "collecting");
  const effectiveUserLang = state.user_lang ?? (locale === "en" ? "en" : "zh");
  const effectiveMerchantLang =
    state.merchant_lang === "zh" || state.merchant_lang === "en"
      ? state.merchant_lang
      : merchantLang === "auto"
        ? null
        : merchantLang;
  const showCrossLangTakeoverNotice =
    takeoverControlsEnabled &&
    state.user_takeover_active &&
    effectiveMerchantLang !== null &&
    effectiveUserLang !== effectiveMerchantLang;
  const copy = focusedCopy(locale);
  const taskText = state.task_description?.trim() || copy.emptyTask;
  const readinessPercent = Math.round(
    Math.max(0, Math.min(1, state.readiness_confidence)) * 100,
  );
  const readinessStatus = state.readiness_passed
    ? t("readiness.ready")
    : t("readiness.waiting");
  const missingCriticalText = state.readiness_missing_critical.length > 0
    ? state.readiness_missing_critical.join(", ")
    : copy.noMissingCritical;
  const outcomeText = state.completion_summary?.trim() ||
    (state.phase === "post_call_review"
      ? copy.reviewReady
      : copy.outcomePending);
  const activeTranscriptCount = state.transcripts.filter(
    (message) => message.subtype !== "translation",
  ).length;

  useEffect(() => {
    if (
      state.ai_active_status !== "filler" &&
      state.ai_active_status !== "keepalive"
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      dispatch({ type: "ai_status_changed", status: null });
    }, 14000);
    return () => window.clearTimeout(timer);
  }, [state.ai_active_status, state.transcripts.length, dispatch]);

  useEffect(() => {
    if (state.phase === "ready_to_dial") {
      setHandoverSheetOpen(true);
      return;
    }
    if (
      isCallPhase ||
      state.phase === "post_call_review" ||
      state.phase === "completed" ||
      state.phase === "failed"
    ) {
      setHandoverSheetOpen(false);
      setMicError(null);
    }
  }, [isCallPhase, state.phase]);

  const main = useMemo(() => {
    if (
      state.phase === "draft" ||
      state.phase === "task_planning" ||
      state.phase === "collecting" ||
      state.phase === "ready_to_dial"
    ) {
      return (
        <>
          <PreflightChat
            transcripts={state.preflight_history.length > 0
              ? state.preflight_history
              : state.transcripts}
            localInputs={state.preflight_local_inputs}
          />
          <TextSupplementInput
            onSend={sendText}
            mode="default"
            phase={state.phase}
            userLang={state.user_lang}
          />
        </>
      );
    }
    if (isCallPhase) {
      return (
        <TranscriptStream
          transcripts={state.transcripts}
          debug={debug}
          autoTranslate={state.auto_translate_merchant}
          userLang={state.user_lang ?? "zh"}
          merchantLang={merchantLang === "auto" ? undefined : merchantLang}
          onDemandTranslate={onDemandTranslate}
          translationsPending={state.translations_pending}
          aiStatus={state.ai_active_status}
        />
      );
    }
    if (state.phase === "post_call_review") {
      const callSegments = state.call_segments.map<ReviewCallSegment>(segment => ({
        ...segment,
        transcript: state.transcripts.filter(m => m.segment_id === segment.id),
      }));
      return (
        <PostCallReview
          assumptions={state.uncertain_assumptions}
          callbacks={state.pending_callbacks}
          call_segments={callSegments}
          status={deriveReviewStatus(callSegments)}
          onConfirm={onConfirm}
          onCorrect={onCorrect}
          onTriggerCallback={onTriggerCallback}
          onCancelCallback={onCancelCallback}
          onRestoreCallback={onRestoreCallback}
          onStartNewCall={onStartNewCall}
          onDismiss={onDismiss}
        />
      );
    }
    // completed | failed — terminal acknowledgement.
    return (
      <section className="card stack live-page__terminal">
        <h2>
          {state.phase === "completed"
            ? t("post_call_review.empty_state")
            : t("errors.unknown")}
        </h2>
        <button
          type="button"
          className="chip-btn chip-btn--primary"
          onClick={onDismiss}
        >
          {t("post_call_review.back")}
        </button>
      </section>
    );
    // Render decisions are pure functions of state we already depend on.
  }, [
    state,
    isCallPhase,
    sendText,
    debug,
    merchantLang,
    onDemandTranslate,
    onConfirm,
    onCorrect,
    onTriggerCallback,
    onCancelCallback,
    onRestoreCallback,
    onStartNewCall,
    onDismiss,
    t,
  ]);

  return (
    <main id="main" className="app-shell live-page">
      {showRecoveredToast ? (
        <SessionRecoveredToast onDismiss={() => setShowRecoveredToast(false)} />
      ) : null}

      <header className="focused-header live-page__topbar">
        <a className="focused-brand" href={`/${locale}/`} aria-label="VocalizeAI home">
          <span className="focused-brand__mark">V</span>
          <span>
            <strong>{t("appName")}</strong>
            <small>{copy.headerSubtitle}</small>
          </span>
        </a>
        <span className="focused-status">
          {readinessStatus} · {phaseLabel(state.phase, locale)}
        </span>
        <div className="focused-header__actions">
          <MerchantLangBadge value={merchantLang} onChange={setMerchantLang} />
          <LanguageToggle />
          <button
            type="button"
            className="focused-icon-button"
            aria-label={copy.doctor}
          >
            <Stethoscope aria-hidden size={17} strokeWidth={2} />
          </button>
          <button
            type="button"
            className="focused-icon-button"
            aria-label={t("settings.title")}
            onClick={() => setSettingsOpen(true)}
          >
            <Settings2 aria-hidden size={17} strokeWidth={2} />
          </button>
        </div>
      </header>

      <section className="focused-hero">
        <div className="focused-task">
          <span className="focused-section-label">{copy.currentTask}</span>
          <h1>{taskText}</h1>
          <div className="focused-task-row">
            <span>{copy.taskHint}</span>
            {state.phase === "ready_to_dial" ? (
              <button
                type="button"
                className="focused-button focused-button--primary"
                aria-label={copy.openHandover}
                onClick={() => setHandoverSheetOpen(true)}
                disabled={!state.readiness_passed}
              >
                <PhoneCall aria-hidden size={17} strokeWidth={2} />
                {t("handover.takeover_button")}
              </button>
            ) : null}
          </div>
        </div>
        <aside className="focused-readiness" aria-label={copy.readiness}>
          <div className="focused-panel-heading">
            <span>
              <Activity aria-hidden size={17} strokeWidth={2} />
              {copy.readiness}
            </span>
            <strong>{readinessPercent}%</strong>
          </div>
          <ReadinessIndicator
            passed={state.readiness_passed}
            missing_critical={state.readiness_missing_critical}
            confidence={state.readiness_confidence}
          />
          <p>{missingCriticalText}</p>
        </aside>
      </section>

      {error ? (
        <div className="alert alert--bad focused-alert" role="alert">{error}</div>
      ) : null}

      <div className="focused-grid">
        <section className="focused-session" aria-label={copy.liveSession}>
          <div className="focused-panel-heading">
            <span>
              <PhoneCall aria-hidden size={17} strokeWidth={2} />
              {copy.liveSession}
            </span>
            <ConnectionStateChip state={state.connection_state} />
          </div>

          <div className="live-page__main focused-session__body">{main}</div>

          {isCallPhase ? (
            <footer className="live-page__footer focused-call-controls">
              {takeoverControlsEnabled ? (
                <UserTakeoverButton
                  active={state.user_takeover_active}
                  onToggle={onUserTakeoverToggle}
                />
              ) : null}
              {showCrossLangTakeoverNotice ? (
                <p className="alert alert--warn live-page__takeover-notice" role="note">
                  {t("user_takeover.relay_hint")}
                </p>
              ) : null}
              <TextSupplementInput
                onSend={sendText}
                phase={state.phase}
                userLang={state.user_lang}
                mode={
                  takeoverControlsEnabled && state.user_takeover_active
                    ? "user_takeover"
                    : "default"
                }
              />
              <HangupButton onConfirm={onHangup} />
            </footer>
          ) : null}
        </section>

        <aside className="focused-side" aria-label={copy.outcomeAndDiagnostics}>
          <section className="focused-panel">
            <div className="focused-panel-heading">
              <span>
                <CheckCircle2 aria-hidden size={17} strokeWidth={2} />
                {copy.outcome}
              </span>
              <strong>{phaseLabel(state.phase, locale)}</strong>
            </div>
            <p className="focused-outcome">{outcomeText}</p>
            <div className="focused-metric-list">
              <span>{copy.assumptions}<strong>{state.uncertain_assumptions.length}</strong></span>
              <span>{copy.callbacks}<strong>{state.pending_callbacks.length}</strong></span>
              <span>{copy.transcripts}<strong>{activeTranscriptCount}</strong></span>
            </div>
          </section>

          <section className="focused-panel">
            <div className="focused-panel-heading">
              <span>
                <Activity aria-hidden size={17} strokeWidth={2} />
                {copy.diagnostics}
              </span>
            </div>
            <dl className="focused-diagnostics">
              <div>
                <dt>{copy.provider}</dt>
                <dd>{copy.providerValue}</dd>
              </div>
              <div>
                <dt>{copy.connection}</dt>
                <dd>{state.connection_state}</dd>
              </div>
              <div>
                <dt>{copy.autoTranslate}</dt>
                <dd>{state.auto_translate_merchant ? copy.on : copy.off}</dd>
              </div>
            </dl>
          </section>

          {showSummaryBanner ? (
            <PreflightSummaryBanner
              slots={state.slots}
              preflight_history={state.preflight_history}
            />
          ) : null}
        </aside>
      </div>

      {showHandover ? (
        <HandoverPanel
          onTakeover={onTakeover}
          disabled={!state.readiness_passed}
          busy={micPreparing}
          error={micError}
        />
      ) : null}

      {state.active_clarification ? (
        <ClarificationModal
          request={{
            field: state.active_clarification.field,
            question: state.active_clarification.question,
            lang: state.active_clarification.lang,
            timeout_s: state.active_clarification.timeout_s,
          }}
          onAck={onClarificationAck}
          onTimeout={onClarificationTimeout}
        />
      ) : null}

      <Settings
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        autoTranslate={state.auto_translate_merchant}
        onAutoTranslateChange={onAutoTranslateChange}
        devicePreferences={devicePreferences}
        onDevicePreferencesChange={onDevicePreferencesChange}
        deviceSwitchStatus={deviceSwitchingStatus}
      />

      {/* Always mounted but only audibly active during the call phase. */}
      <div hidden aria-hidden>
        <BrowserAudioBridge
          readinessPassed={state.readiness_passed}
          handover={isCallPhase}
          takeoverActive={takeoverControlsEnabled && state.user_takeover_active}
          ended={state.phase === "completed" || state.phase === "failed"}
          // Cast: VocalizeSocket implements SocketLike; tests pass a stub with
          // the same shape so .sendAudio is callable.
          socket={socketRef.current as unknown as VocalizeSocket | undefined}
          inputDeviceId={devicePreferences.inputId}
          outputDeviceId={devicePreferences.outputId}
          echoCancellation={devicePreferences.aec}
          onDeviceSwitchingChange={onDeviceSwitchingChange}
          onDeviceSwitchSuccess={onDeviceSwitchSuccess}
          onDeviceSwitchError={onDeviceSwitchError}
          onSpeakerFallback={onSpeakerFallback}
          playbackFrames={audioFrames}
          onPlaybackFramesConsumed={onPlaybackFramesConsumed}
        />
      </div>
    </main>
  );
}

function isPreflight(phase: string): boolean {
  return (
    phase === "draft" ||
    phase === "task_planning" ||
    phase === "collecting" ||
    phase === "ready_to_dial"
  );
}

function phaseLabel(phase: string, locale: string): string {
  const zh: Record<string, string> = {
    draft: "草稿",
    task_planning: "任务规划",
    collecting: "收集信息",
    ready_to_dial: "可接管",
    execution_active: "通话中",
    needs_clarification: "需要确认",
    await_user_clarification: "等待回复",
    post_call_review: "复盘",
    callback_active: "回拨中",
    completed: "完成",
    failed: "失败",
  };
  const en: Record<string, string> = {
    draft: "Draft",
    task_planning: "Planning",
    collecting: "Collecting",
    ready_to_dial: "Ready",
    execution_active: "Live",
    needs_clarification: "Clarify",
    await_user_clarification: "Waiting",
    post_call_review: "Review",
    callback_active: "Callback",
    completed: "Complete",
    failed: "Failed",
  };
  return (locale === "en" ? en : zh)[phase] ?? phase;
}

function focusedCopy(locale: string) {
  if (locale === "en") {
    return {
      headerSubtitle: "Local speech · LLM",
      currentTask: "Task",
      emptyTask: "Enter the goal and required details.",
      taskHint: "Missing fields appear on the right.",
      readiness: "Readiness",
      noMissingCritical: "No critical fields are missing.",
      reviewReady: "Review the result.",
      outcomePending: "Waiting for result.",
      liveSession: "Live session",
      outcomeAndDiagnostics: "Result",
      outcome: "Outcome",
      assumptions: "Checks",
      callbacks: "Callbacks",
      transcripts: "Turns",
      diagnostics: "Diagnostics",
      provider: "Speech",
      providerValue: "Mac",
      connection: "Connection",
      autoTranslate: "Auto translate",
      on: "On",
      off: "Off",
      doctor: "Doctor",
      openHandover: "Open handover",
    };
  }
  return {
    headerSubtitle: "本机语音 · LLM",
    currentTask: "任务",
    emptyTask: "输入目标和必要条件。",
    taskHint: "缺项会显示在右侧。",
    readiness: "准备",
    noMissingCritical: "没有缺失的关键信息。",
    reviewReady: "核对结果。",
    outcomePending: "等待结果。",
    liveSession: "实时会话",
    outcomeAndDiagnostics: "结果",
    outcome: "结果",
    assumptions: "核对项",
    callbacks: "回拨",
    transcripts: "轮次",
    diagnostics: "诊断",
    provider: "语音",
    providerValue: "Mac",
    connection: "连接",
    autoTranslate: "自动翻译",
    on: "开",
    off: "关",
    doctor: "诊断",
    openHandover: "打开交接",
  };
}
