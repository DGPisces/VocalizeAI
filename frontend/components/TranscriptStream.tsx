// frontend/components/TranscriptStream.tsx

import React, { useEffect, useMemo, useRef } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Clock } from "lucide-react";
import type { TranscriptMessage } from "../lib/state";

interface Props {
  transcripts: TranscriptMessage[];
  debug?: boolean;
  translationsPending?: string[];   // parent_ids whose translation hasn't arrived yet
  autoTranslate?: boolean;
  userLang?: "zh" | "en";
  merchantLang?: "zh" | "en";
  onDemandTranslate?: (id: string) => void;
  aiStatus?: "filler" | "keepalive" | "escalation" | null;
  segmentId?: string;
  readOnly?: boolean;
}

export function TranscriptStream({
  transcripts,
  debug = false,
  translationsPending = [],
  autoTranslate,
  userLang,
  merchantLang,
  onDemandTranslate,
  aiStatus = null,
  segmentId,
  readOnly = false,
}: Props) {
  const t = useTranslations("transcript_stream");
  const aiStatusT = useTranslations("ai_status");
  const tailRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    tailRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [transcripts.length]);

  // Group: originals + their child translations (by parent_id)
  const tree = useMemo(() => {
    const byParent = new Map<string, TranscriptMessage[]>();
    const originals: TranscriptMessage[] = [];
    for (const m of transcripts) {
      if (segmentId && m.segment_id !== segmentId) {
        continue;
      }
      if (m.subtype === "translation" && m.parent_id) {
        const arr = byParent.get(m.parent_id) ?? [];
        arr.push(m);
        byParent.set(m.parent_id, arr);
      } else {
        originals.push(m);
      }
    }
    return { originals, byParent };
  }, [transcripts, segmentId]);

  return (
    <section className="transcript-stream" aria-label={t("aria_label")}>
      {aiStatus ? (
        <AiStatusChip status={aiStatus} t={aiStatusT} />
      ) : null}
      <ol className="bubbles">
        {tree.originals.map(msg => {
          if (msg.role === "system" && !debug) return null;
          const translation = msg.parent_id == null ? tree.byParent.get(msg.id) : undefined;
          const isCallbackSegmentBoundary = msg.subtype === "callback_segment";
          const effectiveMerchantLang = merchantLang ?? msg.lang;
          const showTranslateBtn =
            autoTranslate === false &&
            !readOnly &&
            userLang &&
            effectiveMerchantLang &&
            userLang !== effectiveMerchantLang &&
            msg.role === "merchant_to_ai" &&
            !(translation && translation.length > 0) &&
            !translationsPending.includes(msg.id);
          return (
            <li key={msg.id} className={bubbleClass(msg)}>
              {isCallbackSegmentBoundary && (
                <div className="callback-separator" role="separator" aria-label={t("callback_label")}>
                  <span>{t("callback_label")}</span>
                </div>
              )}
              {msg.role === "user_supplement" && (
                <span className="bubble__label">{t("supplement_label")}</span>
              )}
              {msg.role === "user_takeover_passthrough" && (
                <span className="bubble__label">{t("takeover_label")}</span>
              )}
              <p className="bubble__text">{msg.text}</p>
              {translation && translation.length > 0 && (
                <p className="bubble__translation">{translation[0].text}</p>
              )}
              {translationsPending.includes(msg.id) && !translation && (
                <p className="bubble__translation skeleton-text" aria-hidden>……</p>
              )}
              {showTranslateBtn && onDemandTranslate && (
                <button
                  type="button"
                  className="chip-btn bubble__translate-btn"
                  onClick={() => onDemandTranslate(msg.id)}
                >
                  {t("translate_button")}
                </button>
              )}
            </li>
          );
        })}
      </ol>
      <div ref={tailRef} aria-hidden />
    </section>
  );
}

function AiStatusChip({
  status,
  t,
}: {
  status: "filler" | "keepalive" | "escalation";
  t: ReturnType<typeof useTranslations>;
}) {
  const isEscalation = status === "escalation";
  const Icon = isEscalation ? AlertTriangle : Clock;
  const key =
    status === "filler"
      ? "filler_active"
      : status === "keepalive"
        ? "keepalive_active"
        : "escalation_warning";

  return (
    <div
      className={`ai-status-chip ${
        isEscalation ? "ai-status-chip--bad" : "ai-status-chip--warn"
      }`}
      role="status"
      aria-live="polite"
    >
      <Icon size={12} aria-hidden="true" />
      <span>{t(key)}</span>
    </div>
  );
}

function bubbleClass(m: TranscriptMessage): string {
  if (m.subtype === "callback_segment") return "bubble bubble--callback";
  switch (m.role) {
    case "ai_to_merchant": return "bubble bubble--ai-to-merchant";
    case "merchant_to_ai": return "bubble bubble--merchant-to-ai";
    case "user_supplement": return "bubble bubble--user-supplement";
    case "user_takeover_passthrough": return "bubble bubble--user-takeover";
    case "system": return "bubble bubble--system";
    default: return "bubble";
  }
}
