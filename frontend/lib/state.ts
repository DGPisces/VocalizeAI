import { useReducer } from "react";
import type { Dispatch } from "react";
import type { TaskPhaseValue, BackendMode, ServerFrame } from "./ws";

export type Readiness = {
  passed: boolean;
  missingCritical: string[];
  confidence: number;
};

export const initialReadiness: Readiness = {
  passed: false,
  missingCritical: [],
  confidence: 0
};

export type SlotAssumptionSource = "user_timeout" | "merchant_impatience";
export type SlotAssumptionStatus = "pending_review" | "confirmed" | "corrected";

export interface SlotAssumption {
  id: string;
  slot: string;
  question: string;
  assumed_value: unknown;
  source: SlotAssumptionSource;
  created_at: string;            // ISO 8601
  status: SlotAssumptionStatus;
  correction: string | null;
  note: string | null;
  callback_id: string | null;
}

export type CallbackStatus =
  | "queued"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled"
  | "triggered";

export interface CallbackEntry {
  id: string;
  assumption_id: string;
  correction: string;
  note: string | null;
  status: CallbackStatus;
  created_at: string;            // ISO 8601
  started_at: string | null;     // ISO 8601
  completed_at: string | null;   // ISO 8601
  transcript_segment_id: string | null;
}

export interface CallSegment {
  id: string;
  index: number;
  started_at: string;
  ended_at: string | null;
  interrupted: boolean;
  interrupt_reason: "ws_close" | "user_hangup" | "merchant_impatience" | null;
}

export type TranscriptMessageRole =
  | "ai_to_user"
  | "ai_to_merchant"
  | "merchant_to_ai"
  | "user_supplement"
  | "user_takeover_passthrough"
  | "system";

export type TranscriptMessageSubtype =
  | "original"
  | "translation"
  | "user_supplement"
  | "user_takeover_passthrough"
  | "callback_segment"
  | "filler"
  | "keepalive";

export interface TranscriptMessage {
  id: string;
  role: TranscriptMessageRole;
  text: string;
  lang: "zh" | "en" | null;
  is_final: boolean;
  subtype: TranscriptMessageSubtype;
  parent_id: string | null;
  segment_id: string | null;
  created_at: string;            // ISO 8601
}

// ---------------------------------------------------------------------------
// Session reducer
// ---------------------------------------------------------------------------

export interface SessionState {
  phase: TaskPhaseValue;
  transcripts: TranscriptMessage[];
  translations_pending: string[];
  uncertain_assumptions: SlotAssumption[];
  pending_callbacks: CallbackEntry[];
  call_segments: CallSegment[];
  ai_active_status: "filler" | "keepalive" | "escalation" | null;
  connection_state: "connected" | "reconnecting" | "disconnected";
  auto_translate_merchant: boolean;
  readiness_passed: boolean;
  readiness_missing_critical: string[];
  readiness_confidence: number;
  current_mode: BackendMode;
  user_takeover_active: boolean;
  last_error: { code: number; message_zh: string; message_en: string } | null;
  last_escalation: { reason: string; holds_used: number; message_zh: string; message_en: string } | null;
  // Active clarification (P1.3 fix). Null when no modal should render.
  active_clarification: {
    field: string;
    question: string;
    lang: "zh" | "en";
    timeout_s: number;
    received_at: number;     // performance.now()
  } | null;
  // Slot map / preflight Q&A history (P2.5 fix).
  slots: Record<string, unknown>;
  task_description: string | null;
  merchant_lang: "zh" | "en" | "auto" | null;
  user_lang: "zh" | "en" | null;
  completion_summary: string | null;
  preflight_history: TranscriptMessage[];
  // Local user inputs typed during preflight; merged with transcripts at render time.
  preflight_local_inputs: { id: string; text: string; ts: string }[];
}

export type SessionAction =
  | { type: "append_transcript"; message: TranscriptMessage }
  | { type: "translation_pending_mark"; id: string }
  | { type: "translation_pending_clear"; id: string }
  | { type: "translation_pending_clear_all" }
  | { type: "phase_change"; previous: TaskPhaseValue; current: TaskPhaseValue }
  | { type: "uncertain_assumption_added"; assumption: SlotAssumption }
  | { type: "pending_callback_added"; callback: CallbackEntry }
  | { type: "call_segment_added"; segment: CallSegment }
  | { type: "segment_interrupted"; segment_id: string; reason: "ws_close" | "user_hangup" | "merchant_impatience" }
  | { type: "connection_state_changed"; state: "connected" | "reconnecting" | "disconnected" }
  | { type: "ai_status_changed"; status: "filler" | "keepalive" | "escalation" | null }
  | { type: "readiness_change"; passed: boolean; missing_critical: string[]; confidence: number }
  | { type: "mode_ack"; mode: BackendMode }
  | { type: "user_takeover_toggle"; active: boolean }
  | { type: "set_auto_translate"; value: boolean }
  | { type: "error"; code: number; message_zh: string; message_en: string }
  | { type: "escalation_warning"; reason: string; holds_used: number; message_zh: string; message_en: string }
  | { type: "clarification_open"; field: string; question: string; lang: "zh" | "en"; timeout_s: number }
  | { type: "clarification_close" }
  | { type: "preflight_local_input_appended"; entry: { id: string; text: string; ts: string } }
  | { type: "slots_diff"; partial: Record<string, unknown> }
  | { type: "cancel_pending_callback"; callback_id: string }
  | { type: "restore_pending_callback"; callback_id: string }
  | { type: "hydrate"; partial: Partial<SessionState> };

const initialState: SessionState = {
  phase: "draft",
  transcripts: [],
  translations_pending: [],
  uncertain_assumptions: [],
  pending_callbacks: [],
  call_segments: [],
  ai_active_status: null,
  connection_state: "connected",
  auto_translate_merchant: true,
  readiness_passed: false,
  readiness_missing_critical: [],
  readiness_confidence: 0,
  current_mode: "preflight",
  user_takeover_active: false,
  last_error: null,
  last_escalation: null,
  active_clarification: null,
  slots: {},
  task_description: null,
  merchant_lang: null,
  user_lang: null,
  completion_summary: null,
  preflight_history: [],
  preflight_local_inputs: [],
};

function reducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "append_transcript": {
      const idx = state.transcripts.findIndex(t => t.id === action.message.id);
      if (idx >= 0) {
        const next = [...state.transcripts];
        next[idx] = action.message;
        return { ...state, transcripts: next };
      }
      return { ...state, transcripts: [...state.transcripts, action.message] };
    }
    case "translation_pending_mark":
      return state.translations_pending.includes(action.id)
        ? state
        : {
            ...state,
            translations_pending: [...state.translations_pending, action.id],
          };
    case "translation_pending_clear":
      return {
        ...state,
        translations_pending: state.translations_pending.filter(
          id => id !== action.id,
        ),
      };
    case "translation_pending_clear_all":
      return { ...state, translations_pending: [] };
    case "phase_change": {
      const keepsClarification =
        action.current === "needs_clarification" ||
        action.current === "await_user_clarification";
      return {
        ...state,
        phase: action.current,
        active_clarification: keepsClarification
          ? state.active_clarification
          : null,
        ai_active_status: action.current === "post_call_review"
          ? null
          : state.ai_active_status,
      };
    }
    case "uncertain_assumption_added":
      return {
        ...state,
        uncertain_assumptions: [...state.uncertain_assumptions, action.assumption],
      };
    case "pending_callback_added":
      return {
        ...state,
        pending_callbacks: [
          ...state.pending_callbacks.reduce<CallbackEntry[]>(
            (items, c) => c.id === action.callback.id ? items : [...items, c],
            [],
          ),
          action.callback,
        ],
      };
    case "call_segment_added":
      return {
        ...state,
        call_segments: [...state.call_segments, action.segment],
      };
    case "segment_interrupted":
      return {
        ...state,
        call_segments: state.call_segments.map(segment =>
          segment.id === action.segment_id
            ? {
                ...segment,
                interrupted: true,
                interrupt_reason: action.reason,
              }
            : segment,
        ),
      };
    case "connection_state_changed":
      return { ...state, connection_state: action.state };
    case "ai_status_changed":
      if (
        state.ai_active_status === "escalation" &&
        (action.status === "filler" || action.status === "keepalive")
      ) {
        return state;
      }
      return { ...state, ai_active_status: action.status };
    case "readiness_change":
      return {
        ...state,
        readiness_passed: action.passed,
        readiness_missing_critical: action.missing_critical,
        readiness_confidence: action.confidence,
      };
    case "mode_ack":
      return {
        ...state,
        current_mode: action.mode,
        user_takeover_active: action.mode === "user_takeover",
      };
    case "user_takeover_toggle":
      return { ...state, user_takeover_active: action.active };
    case "set_auto_translate":
      return { ...state, auto_translate_merchant: action.value };
    case "error":
      return {
        ...state,
        last_error: {
          code: action.code,
          message_zh: action.message_zh,
          message_en: action.message_en,
        },
      };
    case "escalation_warning":
      return {
        ...state,
        ai_active_status: "escalation",
        last_escalation: {
          reason: action.reason,
          holds_used: action.holds_used,
          message_zh: action.message_zh,
          message_en: action.message_en,
        },
      };
    case "clarification_open":
      return {
        ...state,
        active_clarification: {
          field: action.field,
          question: action.question,
          lang: action.lang,
          timeout_s: action.timeout_s,
          received_at: performance.now(),
        },
      };
    case "clarification_close":
      return { ...state, active_clarification: null };
    case "preflight_local_input_appended":
      return {
        ...state,
        preflight_local_inputs: [...state.preflight_local_inputs, action.entry],
      };
    case "slots_diff":
      return { ...state, slots: { ...state.slots, ...action.partial } };
    case "cancel_pending_callback":
      return {
        ...state,
        pending_callbacks: state.pending_callbacks.map(
          c => c.id === action.callback_id ? { ...c, status: "cancelled" } : c,
        ),
      };
    case "restore_pending_callback":
      return {
        ...state,
        pending_callbacks: state.pending_callbacks.map(
          c => c.id === action.callback_id && c.status === "cancelled"
            ? { ...c, status: "queued" }
            : c,
        ),
      };
    case "hydrate":
      return { ...state, ...action.partial };
    default: {
      const _: never = action;
      return state;
    }
  }
}

export function useSessionStore() {
  const [state, dispatch] = useReducer(reducer, initialState);
  return { state, dispatch };
}

// ---------------------------------------------------------------------------
// applyServerFrame — WS frame → reducer action adapter
// Centralises the ServerFrame → SessionAction mapping so every socket
// consumer uses the same translation path.
// ---------------------------------------------------------------------------

export function applyServerFrame(
  dispatch: Dispatch<SessionAction>,
  frame: ServerFrame,
): void {
  switch (frame.type) {
    case "transcript_update":
      dispatch({
        type: "append_transcript",
        message: {
          id: frame.id,
          role: frame.role,
          text: frame.text,
          lang: frame.lang,
          is_final: frame.is_final,
          subtype: frame.subtype,
          parent_id: frame.parent_id,
          segment_id: frame.segment_id,
          created_at: frame.created_at,
        },
      });
      if (frame.subtype === "translation" && frame.parent_id) {
        dispatch({
          type: "translation_pending_clear",
          id: frame.parent_id,
        });
      }
      if (frame.subtype === "filler" || frame.subtype === "keepalive") {
        dispatch({ type: "ai_status_changed", status: frame.subtype });
      }
      return;
    case "phase_change":
      dispatch({ type: "phase_change", previous: frame.previous, current: frame.current });
      return;
    case "uncertain_assumption_added":
      dispatch({ type: "uncertain_assumption_added", assumption: frame.assumption });
      return;
    case "pending_callback_added":
      dispatch({ type: "pending_callback_added", callback: frame.callback });
      return;
    case "call_segment_added":
      dispatch({ type: "call_segment_added", segment: frame.segment });
      return;
    case "segment_interrupted":
      dispatch({
        type: "segment_interrupted",
        segment_id: frame.segment_id,
        reason: frame.reason,
      });
      return;
    case "readiness_change":
      dispatch({
        type: "readiness_change",
        passed: frame.passed,
        missing_critical: frame.missing_critical,
        confidence: frame.confidence,
      });
      return;
    case "mode_ack":
      dispatch({ type: "mode_ack", mode: frame.mode });
      return;
    case "error":
      dispatch({
        type: "error",
        code: frame.code,
        message_zh: frame.message_zh,
        message_en: frame.message_en,
      });
      dispatch({ type: "translation_pending_clear_all" });
      return;
    case "escalation_warning":
      dispatch({
        type: "escalation_warning",
        reason: frame.reason,
        holds_used: frame.holds_used,
        message_zh: frame.message_zh,
        message_en: frame.message_en,
      });
      dispatch({ type: "ai_status_changed", status: "escalation" });
      return;
    case "state_update": {
      // Surface auto_translate_merchant toggles; other diff keys are
      // debug-only (inspected in TranscriptStream when ?debug=1).
      // Naming note (P1.11 fix): backend J4 emits the diff key
      // `auto_translate_merchant` to match TaskState's field name.
      const v = frame.diff?.auto_translate_merchant;
      if (typeof v === "boolean") {
        dispatch({ type: "set_auto_translate", value: v });
      }
      const partial: Partial<SessionState> = {};
      const callbacks = frame.diff?.pending_callbacks;
      if (Array.isArray(callbacks)) {
        partial.pending_callbacks = callbacks as CallbackEntry[];
      }
      const assumptions = frame.diff?.uncertain_assumptions;
      if (Array.isArray(assumptions)) {
        partial.uncertain_assumptions = assumptions as SlotAssumption[];
      }
      const slots = frame.diff?.slots;
      if (slots && typeof slots === "object" && !Array.isArray(slots)) {
        const slotPatch = slots as Record<string, unknown>;
        dispatch({ type: "slots_diff", partial: slotPatch });
        const slotMerchantLang = slotPatch.merchant_lang;
        if (slotMerchantLang === "zh" || slotMerchantLang === "en") {
          partial.merchant_lang = slotMerchantLang;
        }
      }
      const merchantLang = frame.diff?.merchant_lang;
      if (merchantLang === "zh" || merchantLang === "en") {
        partial.merchant_lang = merchantLang;
      }
      const userLang = frame.diff?.user_lang;
      if (userLang === "zh" || userLang === "en") {
        partial.user_lang = userLang;
      }
      const phase = frame.diff?.phase;
      if (isTaskPhaseValue(phase)) {
        partial.phase = phase;
      }
      const summary = frame.diff?.summary;
      if (typeof summary === "string" && summary.trim()) {
        partial.completion_summary = summary;
      }
      if (Object.keys(partial).length > 0) {
        dispatch({ type: "hydrate", partial });
      }
      return;
    }
    case "clarification_request":
      // P1.3 fix: route into the reducer slice. The live page (G1) reads
      // state.active_clarification and renders <ClarificationModal>; on ack
      // / timeout / escalation, it dispatches `clarification_close`.
      dispatch({
        type: "clarification_open",
        field: frame.field,
        question: frame.question,
        lang: frame.lang,
        timeout_s: frame.timeout_s,
      });
      return;
    default: {
      const _: never = frame;
      return;
    }
  }
}

function isTaskPhaseValue(value: unknown): value is TaskPhaseValue {
  return (
    value === "draft" ||
    value === "task_planning" ||
    value === "collecting" ||
    value === "ready_to_dial" ||
    value === "execution_active" ||
    value === "needs_clarification" ||
    value === "await_user_clarification" ||
    value === "post_call_review" ||
    value === "callback_active" ||
    value === "completed" ||
    value === "failed"
  );
}
