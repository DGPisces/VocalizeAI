// frontend/components/TextSupplementInput.tsx

import React, { useState } from "react";
import { useTranslations } from "next-intl";
import type { TaskPhaseValue, TextInputMode } from "../lib/ws";

interface Props {
  onSend: (payload: { text: string; lang_hint?: "zh" | "en"; mode: TextInputMode }) => void;
  mode?: TextInputMode;
  phase: TaskPhaseValue;
  userLang?: "zh" | "en" | null;
}

export function TextSupplementInput({ onSend, mode = "default", phase, userLang }: Props) {
  const t = useTranslations("supplement_input");
  const [text, setText] = useState("");
  const placeholderKey = isPreflightPhase(phase)
    ? "placeholder_preflight"
    : mode === "user_takeover"
      ? "placeholder_takeover"
      : "placeholder_default";
  const placeholder = t(placeholderKey);

  function submit() {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend({ text: trimmed, mode, lang_hint: userLang ?? undefined });
    setText("");
  }

  return (
    <form
      className="text-supplement-input"
      onSubmit={e => { e.preventDefault(); submit(); }}
    >
      <input
        type="text"
        placeholder={placeholder}
        value={text}
        onChange={e => setText(e.target.value)}
        aria-label={placeholder}
      />
      <button type="submit" className="chip-btn">
        {t("send")}
      </button>
    </form>
  );
}

function isPreflightPhase(phase: TaskPhaseValue): boolean {
  return (
    phase === "draft" ||
    phase === "task_planning" ||
    phase === "collecting" ||
    phase === "ready_to_dial"
  );
}
