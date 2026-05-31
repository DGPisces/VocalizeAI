// frontend/components/PostCallReview.tsx

import React, { useState } from "react";
import { useTranslations } from "@/src/i18n";
import type { GetReviewResponse, ReviewCallSegment } from "../lib/api";
import type { CallbackEntry, SlotAssumption } from "../lib/state";
import { RedialConfirmModal } from "./RedialConfirmModal";
import { TranscriptStream } from "./TranscriptStream";

interface Props {
  assumptions: SlotAssumption[];
  callbacks: CallbackEntry[];
  call_segments?: ReviewCallSegment[];
  status?: GetReviewResponse["status"];
  completion_summary?: string | null;
  onConfirm?: (assumption_id: string) => void;
  onCorrect?: (p: { assumption_id: string; correction: string; note: string | null }) => void;
  onConfirmAssumption?: (assumption_id: string, confirmed_value: unknown | null) => void;
  onTriggerCallback?: (callback_id: string) => void;
  onCancelCallback?: (callback_id: string) => void;
  onRestoreCallback?: (callback_id: string) => void;
  onStartNewCall?: () => void;
  onDismiss?: () => void;
}

export function PostCallReview(props: Props) {
  const t = useTranslations("post_call_review");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const queuedCallbacks = props.callbacks.filter(cb => cb.status === "queued");
  const cancelledCallbacks = props.callbacks.filter(cb => cb.status === "cancelled");
  const otherCallbacks = props.callbacks.filter(
    cb => cb.status !== "queued" && cb.status !== "cancelled",
  );
  const hasReviewContent =
    props.assumptions.length > 0 ||
    props.callbacks.length > 0 ||
    (props.call_segments?.length ?? 0) > 0;

  if (!hasReviewContent) {
    return (
      <section className="post-call-review post-call-review--empty">
        <h2>{t("empty_state")}</h2>
        <button
          type="button"
          className="chip-btn"
          onClick={props.onDismiss}
          disabled={!props.onDismiss}
        >
          {t("back")}
        </button>
      </section>
    );
  }

  return (
    <section className="post-call-review">
      <div className="post-call-review__header">
        {props.status ? <StatusChip status={props.status} /> : null}
        <h2>{t("title")}</h2>
        {props.onStartNewCall ? (
          <button
            type="button"
            className="chip-btn chip-btn--primary"
            onClick={() => setConfirmOpen(true)}
          >
            {t("start_new_call")}
          </button>
        ) : null}
      </div>
      {props.completion_summary ? (
        <p className="post-call-review__summary">{props.completion_summary}</p>
      ) : null}
      {(props.call_segments?.length ?? 0) > 0 ? (
        <section className="review-segments" aria-label={t("segments")}>
          {props.call_segments?.map(segment => (
            <details key={segment.id} className="review-segment">
              <summary>
                <span>{t("segment_title", { index: segment.index })}</span>
                <time>{formatTime(segment.started_at)}</time>
              </summary>
              {segment.interrupted ? (
                <p className="review-segment__banner">
                  {t("segment_interrupted", {
                    time: formatTime(segment.ended_at ?? segment.started_at),
                  })}
                </p>
              ) : null}
              <TranscriptStream
                transcripts={segment.transcript}
                segmentId={segment.id}
                readOnly
              />
            </details>
          ))}
        </section>
      ) : null}
      {props.assumptions.length > 0 ? (
        <ol className="assumptions">
          {props.assumptions.map(a => (
            <AssumptionRow
              key={a.id}
              assumption={a}
              onConfirm={props.onConfirm}
              onCorrect={props.onCorrect}
              onConfirmAssumption={props.onConfirmAssumption}
            />
          ))}
        </ol>
      ) : null}
      {queuedCallbacks.length > 0 || cancelledCallbacks.length > 0 || otherCallbacks.length > 0 ? (
        <>
          <h3>{t("pending_callbacks")}</h3>
          <ol className="callbacks">
            {queuedCallbacks.map(cb => (
              <CallbackRow
                key={cb.id}
                callback={cb}
                onTriggerCallback={props.onTriggerCallback}
                onCancelCallback={props.onCancelCallback}
              />
            ))}
            {otherCallbacks.map(cb => (
              <CallbackRow key={cb.id} callback={cb} />
            ))}
            {cancelledCallbacks.map(cb => (
              <CallbackRow
                key={cb.id}
                callback={cb}
                onRestoreCallback={props.onRestoreCallback}
              />
            ))}
          </ol>
        </>
      ) : null}
      {confirmOpen ? (
        <RedialConfirmModal
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false);
            props.onStartNewCall?.();
          }}
        />
      ) : null}
    </section>
  );
}

function StatusChip({ status }: { status: GetReviewResponse["status"] }) {
  const t = useTranslations("post_call_review");
  const cls =
    status === "completed"
      ? "status-chip status-chip--good-soft"
      : status === "interrupted"
        ? "status-chip status-chip--warn-soft"
        : "status-chip status-chip--bad-soft";
  return <span className={cls}>{t(`status_${status}`)}</span>;
}

function AssumptionRow({
  assumption,
  onConfirm,
  onCorrect,
  onConfirmAssumption,
}: {
  assumption: SlotAssumption;
  onConfirm?: Props["onConfirm"];
  onCorrect?: Props["onCorrect"];
  onConfirmAssumption?: Props["onConfirmAssumption"];
}) {
  const t = useTranslations("post_call_review");
  const [expanded, setExpanded] = useState(false);
  const [correction, setCorrection] = useState("");
  const [note, setNote] = useState("");

  const correctValueId = `correct-value-${assumption.id}`;
  const noteId = `note-${assumption.id}`;
  const isFinal = assumption.status !== "pending_review";
  const displayValue = assumption.correction ?? String(assumption.assumed_value ?? "");

  return (
    <li className="assumption-row">
      <p>
        <strong>{assumption.slot}</strong>: {displayValue}
      </p>
      {!isFinal && (
        !expanded ? (
          <div className="assumption-row__actions">
            <button
              type="button"
              className="chip-btn chip-btn--primary"
              onClick={() => {
                onConfirmAssumption?.(assumption.id, null);
                onConfirm?.(assumption.id);
              }}
            >
              {t("confirm_correct")}
            </button>
            <button type="button" className="chip-btn" onClick={() => setExpanded(true)}>
              {t("flag_wrong")}
            </button>
          </div>
        ) : (
          <form
            className="assumption-row__form"
            onSubmit={e => {
              e.preventDefault();
              onConfirmAssumption?.(assumption.id, correction);
              onCorrect?.({
                assumption_id: assumption.id,
                correction,
                note: note || null,
              });
            }}
          >
            <label htmlFor={correctValueId}>{t("correct_value")}</label>
            <input
              id={correctValueId}
              type="text"
              value={correction}
              onChange={e => setCorrection(e.target.value)}
              required
            />
            <label htmlFor={noteId}>{t("note_optional")}</label>
            <input
              id={noteId}
              type="text"
              value={note}
              onChange={e => setNote(e.target.value)}
            />
            <button type="submit" className="chip-btn chip-btn--primary">
              {t("submit_correction")}
            </button>
            <button type="button" className="chip-btn" onClick={() => setExpanded(false)}>
              {t("cancel")}
            </button>
          </form>
        )
      )}
    </li>
  );
}

function CallbackRow({
  callback,
  onTriggerCallback,
  onCancelCallback,
  onRestoreCallback,
}: {
  callback: CallbackEntry;
  onTriggerCallback?: Props["onTriggerCallback"];
  onCancelCallback?: Props["onCancelCallback"];
  onRestoreCallback?: Props["onRestoreCallback"];
}) {
  const t = useTranslations("post_call_review");
  const isQueued = callback.status === "queued";
  const isCancelled = callback.status === "cancelled";
  return (
    <li className={`callback-row ${isCancelled ? "callback-row--cancelled" : ""}`}>
      <p className="callback-row__summary">{callback.correction}</p>
      <span className="callback-row__status">{t(`callback_status_${callback.status}`)}</span>
      <div className="callback-row__actions">
        {isQueued ? (
          <>
            <button
              type="button"
              className="chip-btn chip-btn--primary"
              onClick={() => onTriggerCallback?.(callback.id)}
            >
              {t("dial_now")}
            </button>
            <button
              type="button"
              className="chip-btn"
              onClick={() => onCancelCallback?.(callback.id)}
            >
              {t("cancel")}
            </button>
          </>
        ) : null}
        {isCancelled ? (
          <button
            type="button"
            className="chip-btn"
            onClick={() => onRestoreCallback?.(callback.id)}
          >
            {t("undo_cancel")}
          </button>
        ) : null}
      </div>
    </li>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
