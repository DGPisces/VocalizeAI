import { describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { SlotAssumption, CallbackEntry, CallSegment, TranscriptMessage, SessionAction } from "../lib/state";
import { useSessionStore, applyServerFrame } from "../lib/state";
import type { Dispatch } from "react";

describe("B3a shared types", () => {
  it("SlotAssumption has the spec §3.8 shape", () => {
    const sa: SlotAssumption = {
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
    };
    expect(sa.slot).toBe("party_size");
  });

  it("CallbackEntry status is the typed union", () => {
    const cb: CallbackEntry = {
      id: "cb-1",
      assumption_id: "a-1",
      correction: "6",
      note: null,
      status: "queued",
      created_at: "2026-05-07T12:00:00Z",
      started_at: null,
      completed_at: null,
      transcript_segment_id: null,
    };
    expect(cb.status).toBe("queued");
  });

  it("CallSegment mirrors backend lifecycle fields", () => {
    const segment: CallSegment = {
      id: "seg-1",
      index: 1,
      started_at: "2026-05-07T12:00:00Z",
      ended_at: null,
      interrupted: false,
      interrupt_reason: null,
    };
    expect(segment.index).toBe(1);
  });

  it("TranscriptMessage subtype enumerates all five values", () => {
    const subs: TranscriptMessage["subtype"][] = [
      "original",
      "translation",
      "user_supplement",
      "user_takeover_passthrough",
      "callback_segment",
    ];
    expect(subs).toHaveLength(5);
  });
});

describe("useSessionStore", () => {
  it("starts in idle state with empty slices", () => {
    const { result } = renderHook(() => useSessionStore());
    expect(result.current.state.phase).toBe("draft");
    expect(result.current.state.transcripts).toEqual([]);
    expect(result.current.state.uncertain_assumptions).toEqual([]);
    expect(result.current.state.pending_callbacks).toEqual([]);
    expect(result.current.state.auto_translate_merchant).toBe(true);
  });

  it("appendTranscript adds by id; replaces if id repeats", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({
        type: "append_transcript",
        message: { id: "t-1", role: "ai_to_user", text: "hi", lang: "en",
                   is_final: true, subtype: "original", parent_id: null,
                   segment_id: null, created_at: "x" },
      });
    });
    expect(result.current.state.transcripts).toHaveLength(1);
    act(() => {
      result.current.dispatch({
        type: "append_transcript",
        message: { id: "t-1", role: "ai_to_user", text: "hi (edit)", lang: "en",
                   is_final: true, subtype: "original", parent_id: null,
                   segment_id: null, created_at: "x" },
      });
    });
    expect(result.current.state.transcripts).toHaveLength(1);
    expect(result.current.state.transcripts[0].text).toBe("hi (edit)");
  });

  it("phase_change updates phase and emits a state diff", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({
        type: "phase_change",
        previous: "execution_active",
        current: "post_call_review",
      });
    });
    expect(result.current.state.phase).toBe("post_call_review");
  });

  it("phase_change clears active clarification after leaving clarification phases", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({
        type: "clarification_open",
        field: "party_size",
        question: "How many?",
        lang: "en",
        timeout_s: 30,
      });
    });
    expect(result.current.state.active_clarification).not.toBeNull();

    act(() => {
      result.current.dispatch({
        type: "phase_change",
        previous: "await_user_clarification",
        current: "post_call_review",
      });
    });
    expect(result.current.state.active_clarification).toBeNull();
  });

  it("test_phase_change_to_post_call_review_clears_ai_active_status", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({ type: "ai_status_changed", status: "filler" });
      result.current.dispatch({
        type: "phase_change",
        previous: "execution_active",
        current: "post_call_review",
      });
    });
    expect(result.current.state.ai_active_status).toBeNull();
  });

  it("escalation status persists across later filler until post_call_review", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({ type: "ai_status_changed", status: "escalation" });
      result.current.dispatch({ type: "ai_status_changed", status: "filler" });
    });
    expect(result.current.state.ai_active_status).toBe("escalation");
  });

  it("uncertain_assumption_added appends to the list", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({
        type: "uncertain_assumption_added",
        assumption: {
          id: "a-1", slot: "x", question: "?", assumed_value: 1,
          source: "user_timeout", created_at: "x",
          status: "pending_review", correction: null, note: null, callback_id: null,
        },
      });
    });
    expect(result.current.state.uncertain_assumptions).toHaveLength(1);
  });

  it("set_auto_translate toggles the flag", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({ type: "set_auto_translate", value: false });
    });
    expect(result.current.state.auto_translate_merchant).toBe(false);
  });

  it("test_reducer_call_segment_added_appends", () => {
    const { result } = renderHook(() => useSessionStore());
    const segment: CallSegment = {
      id: "seg-1",
      index: 1,
      started_at: "x",
      ended_at: null,
      interrupted: false,
      interrupt_reason: null,
    };
    act(() => {
      result.current.dispatch({ type: "call_segment_added", segment });
    });
    expect(result.current.state.call_segments).toEqual([segment]);
  });

  it("test_reducer_segment_interrupted_marks_segment", () => {
    const { result } = renderHook(() => useSessionStore());
    const segment: CallSegment = {
      id: "seg-1",
      index: 1,
      started_at: "x",
      ended_at: null,
      interrupted: false,
      interrupt_reason: null,
    };
    act(() => {
      result.current.dispatch({ type: "call_segment_added", segment });
      result.current.dispatch({
        type: "segment_interrupted",
        segment_id: "seg-1",
        reason: "ws_close",
      });
    });
    expect(result.current.state.call_segments[0]).toMatchObject({
      interrupted: true,
      interrupt_reason: "ws_close",
    });
  });

  it("test_reducer_connection_state_changed_updates_slot", () => {
    const { result } = renderHook(() => useSessionStore());
    expect(result.current.state.connection_state).toBe("connected");
    act(() => {
      result.current.dispatch({
        type: "connection_state_changed",
        state: "reconnecting",
      });
    });
    expect(result.current.state.connection_state).toBe("reconnecting");
    act(() => {
      result.current.dispatch({
        type: "connection_state_changed",
        state: "connected",
      });
    });
    expect(result.current.state.connection_state).toBe("connected");
  });

  it("test_reducer_cancel_pending_callback_now_flips_status_instead_of_filtering", () => {
    const { result } = renderHook(() => useSessionStore());
    const callback: CallbackEntry = {
      id: "cb-1",
      assumption_id: "a-1",
      correction: "6",
      note: null,
      status: "queued",
      created_at: "x",
      started_at: null,
      completed_at: null,
      transcript_segment_id: null,
    };
    act(() => {
      result.current.dispatch({ type: "pending_callback_added", callback });
      result.current.dispatch({
        type: "cancel_pending_callback",
        callback_id: "cb-1",
      });
    });
    expect(result.current.state.pending_callbacks).toHaveLength(1);
    expect(result.current.state.pending_callbacks[0].status).toBe("cancelled");
    act(() => {
      result.current.dispatch({
        type: "restore_pending_callback",
        callback_id: "cb-1",
      });
    });
    expect(result.current.state.pending_callbacks[0].status).toBe("queued");
  });

  it("tracks and clears pending on-demand translation ids", () => {
    const { result } = renderHook(() => useSessionStore());
    act(() => {
      result.current.dispatch({
        type: "translation_pending_mark",
        id: "m-1",
      } as any);
    });
    expect(result.current.state.translations_pending).toEqual(["m-1"]);

    act(() => {
      result.current.dispatch({
        type: "translation_pending_clear",
        id: "m-1",
      } as any);
    });
    expect(result.current.state.translations_pending).toEqual([]);
  });
});

describe("applyServerFrame", () => {
  it("transcript_update → append_transcript with full identity", () => {
    const calls: SessionAction[] = [];
    const dispatch: Dispatch<SessionAction> = a => calls.push(a);
    applyServerFrame(dispatch, {
      type: "transcript_update", id: "t-1", role: "ai_to_user",
      text: "hi", lang: "en", is_final: true,
      subtype: "original", parent_id: null, segment_id: null,
      created_at: "x",
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].type).toBe("append_transcript");
    if (calls[0].type === "append_transcript") {
      expect(calls[0].message.id).toBe("t-1");
    }
  });

  it("test_apply_server_frame_filler_dispatches_ai_status_filler", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "transcript_update", id: "t-1", role: "ai_to_merchant",
      text: "请稍等", lang: "zh", is_final: true,
      subtype: "filler", parent_id: null, segment_id: "seg-1",
      created_at: "x",
    });
    expect(calls.map(c => c.type)).toEqual(["append_transcript", "ai_status_changed"]);
    expect(calls[1]).toEqual({ type: "ai_status_changed", status: "filler" });
  });

  it("test_apply_server_frame_keepalive_dispatches_ai_status_keepalive", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "transcript_update", id: "t-1", role: "ai_to_merchant",
      text: "正在确认", lang: "zh", is_final: true,
      subtype: "keepalive", parent_id: null, segment_id: "seg-1",
      created_at: "x",
    });
    expect(calls.map(c => c.type)).toEqual(["append_transcript", "ai_status_changed"]);
    expect(calls[1]).toEqual({ type: "ai_status_changed", status: "keepalive" });
  });

  it("test_apply_server_frame_escalation_warning_dispatches_ai_status_escalation", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "escalation_warning",
      reason: "merchant_impatience",
      holds_used: 3,
      message_zh: "x",
      message_en: "x",
    });
    expect(calls).toContainEqual({ type: "ai_status_changed", status: "escalation" });
  });

  it("phase_change → phase_change action", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "phase_change", previous: "execution_active", current: "post_call_review",
    });
    expect(calls[0]).toEqual({
      type: "phase_change", previous: "execution_active", current: "post_call_review",
    });
  });

  it("test_apply_server_frame_routes_call_segment_added_and_segment_interrupted", () => {
    const calls: SessionAction[] = [];
    const segment: CallSegment = {
      id: "seg-1",
      index: 1,
      started_at: "x",
      ended_at: null,
      interrupted: false,
      interrupt_reason: null,
    };
    applyServerFrame(a => calls.push(a), {
      type: "call_segment_added",
      segment,
    });
    applyServerFrame(a => calls.push(a), {
      type: "segment_interrupted",
      segment_id: "seg-1",
      reason: "ws_close",
    });
    expect(calls).toEqual([
      { type: "call_segment_added", segment },
      { type: "segment_interrupted", segment_id: "seg-1", reason: "ws_close" },
    ]);
  });

  it("escalation_warning + state_update.diff.auto_translate_merchant routes correctly", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "escalation_warning", reason: "merchant_impatience",
      holds_used: 2, message_zh: "x", message_en: "x",
    });
    expect(calls[0].type).toBe("escalation_warning");

    calls.length = 0;
    applyServerFrame(a => calls.push(a), {
      type: "state_update", diff: { auto_translate_merchant: false },
    });
    expect(calls[0]).toEqual({ type: "set_auto_translate", value: false });
  });

  it("state_update.diff.uncertain_assumptions hydrates the assumption list", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "state_update",
      diff: {
        uncertain_assumptions: [{
          id: "a-1",
          slot: "party_size",
          question: "How many?",
          assumed_value: 4,
          source: "user_timeout",
          created_at: "x",
          status: "confirmed",
          correction: null,
          note: null,
          callback_id: null,
        }],
      },
    });

    expect(calls[0]).toEqual({
      type: "hydrate",
      partial: {
        uncertain_assumptions: [{
          id: "a-1",
          slot: "party_size",
          question: "How many?",
          assumed_value: 4,
          source: "user_timeout",
          created_at: "x",
          status: "confirmed",
          correction: null,
          note: null,
          callback_id: null,
        }],
      },
    });
  });

  it("state_update.diff.phase and summary hydrate terminal state", () => {
    const calls: SessionAction[] = [];
    applyServerFrame(a => calls.push(a), {
      type: "state_update",
      diff: {
        event: "completed",
        phase: "completed",
        summary: "预订成功：五月十号晚上九点，四位。",
      },
    });

    expect(calls[0]).toEqual({
      type: "hydrate",
      partial: {
        phase: "completed",
        completion_summary: "预订成功：五月十号晚上九点，四位。",
      },
    });
  });
});
