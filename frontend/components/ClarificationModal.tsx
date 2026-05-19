// frontend/components/ClarificationModal.tsx — new

import React, { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

interface Request {
  field: string;
  question: string;
  lang: "zh" | "en";
  timeout_s: number;
}

interface Props {
  request: Request;
  onAck: (slot_value: string) => void;
  onTimeout: () => void;
}

export function ClarificationModal({ request, onAck, onTimeout }: Props) {
  const t = useTranslations("clarification");
  const [expanded, setExpanded] = useState(false);
  const [remaining, setRemaining] = useState(request.timeout_s);
  const [value, setValue] = useState("");
  const [paused, setPaused] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (paused) return;
    timerRef.current = window.setInterval(() => {
      setRemaining(r => {
        if (r <= 1) {
          if (timerRef.current) window.clearInterval(timerRef.current);
          onTimeout();
          return 0;
        }
        return r - 1;
      });
    }, 1000);
    return () => { if (timerRef.current) window.clearInterval(timerRef.current); };
  }, [onTimeout, paused]);

  useEffect(() => {
    if (!expanded) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setPaused(false);
        setExpanded(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  function submit() {
    const v = value.trim();
    if (!v) return;
    onAck(v);
  }

  const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
  const ss = String(remaining % 60).padStart(2, "0");
  const progressPct = Math.max(0, (remaining / request.timeout_s) * 100);

  if (!expanded) {
    return (
      <aside className="clarification-toast" role="complementary">
        <p className="clarification-toast__question">{request.question}</p>
        <div className="clarification-toast__bar progress">
          <div className="progress__fill" style={{ width: `${progressPct}%` }} />
        </div>
        <span className="clarification-toast__countdown">{`${mm}:${ss}`}</span>
        <button onClick={() => setExpanded(true)} className="chip-btn">{t("answer")}</button>
      </aside>
    );
  }

  return (
    <div
      className="clarification-modal-backdrop"
      role="dialog"
      aria-modal="true"
      onClick={e => {
        if (e.target === e.currentTarget) {
          setPaused(false);
          setExpanded(false);
        }
      }}
    >
      <div className="clarification-modal">
        <h2>{request.question}</h2>
        <p className="clarification-modal__countdown">{`${mm}:${ss}`}</p>
        <input
          type="text"
          autoFocus
          value={value}
          onChange={e => {
            const next = e.target.value;
            const pausedByTyping = expanded && next.length > 0;
            setValue(next);
            setPaused(pausedByTyping);
          }}
          aria-label={t("answer_label")}
        />
        <button onClick={submit} className="chip-btn chip-btn--primary">{t("submit")}</button>
      </div>
    </div>
  );
}
