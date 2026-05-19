// frontend/components/PreflightChat.tsx

import React, { useEffect, useMemo, useRef } from "react";
import { useTranslations } from "next-intl";
import type { TranscriptMessage } from "../lib/state";

export interface LocalUserInput {
  id: string;
  text: string;
  ts: string;          // ISO 8601, used for chronological merge
}

interface Props {
  transcripts: TranscriptMessage[];
  localInputs: LocalUserInput[];
}

type Item =
  | { kind: "transcript"; data: TranscriptMessage; ts: string }
  | { kind: "local"; data: LocalUserInput; ts: string };

export function PreflightChat({ transcripts, localInputs }: Props) {
  const t = useTranslations("preflight_chat");
  const tailRef = useRef<HTMLDivElement | null>(null);

  const items = useMemo<Item[]>(() => {
    const xs: Item[] = [
      ...transcripts.map(m => ({ kind: "transcript" as const, data: m, ts: m.created_at })),
      ...localInputs.map(l => ({ kind: "local" as const, data: l, ts: l.ts })),
    ];
    xs.sort((a, b) => a.ts.localeCompare(b.ts));
    return xs;
  }, [transcripts, localInputs]);

  useEffect(() => {
    tailRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items.length]);

  const supplementLabel = t("supplement_label");

  return (
    <section className="preflight-chat" aria-label={t("aria_label")}>
      <ol className="bubbles">
        {items.map(item =>
          item.kind === "transcript" ? (
            <li key={item.data.id} className={bubbleClass(item.data)}>
              <p className="bubble__text">{item.data.text}</p>
            </li>
          ) : (
            // DEVIATION: use bubble--user-supplement (existing class) instead of bubble--user
            <li
              key={item.data.id}
              className="bubble bubble--user-supplement"
              aria-label={supplementLabel}
            >
              <p className="bubble__text">{item.data.text}</p>
            </li>
          ),
        )}
      </ol>
      <div ref={tailRef} aria-hidden />
    </section>
  );
}

// Preflight conversation surfaces only `ai_to_user` (assistant question) and
// `user_supplement` (user-typed clarification) roles. The other roles in
// `TranscriptMessageRole` are call-phase concerns (`ai_to_merchant`,
// `merchant_to_ai`, `user_takeover_passthrough`) or synthetic (`system`); if
// any leak into preflight, the default neutral `.bubble` style avoids a crash
// while still flagging visually that something unexpected appeared.
function bubbleClass(m: TranscriptMessage): string {
  switch (m.role) {
    case "ai_to_user": return "bubble bubble--ai";
    case "user_supplement": return "bubble bubble--user-supplement";
    default: return "bubble";
  }
}
