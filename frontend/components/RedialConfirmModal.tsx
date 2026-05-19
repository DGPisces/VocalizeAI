// frontend/components/RedialConfirmModal.tsx

import React, { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";

interface Props {
  onCancel: () => void;
  onConfirm: () => void;
}

export function RedialConfirmModal({ onCancel, onConfirm }: Props) {
  const t = useTranslations("post_call_review");
  const primaryRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    primaryRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onCancel();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      role="dialog"
      aria-modal={true}
      aria-labelledby="start-new-call-title"
      className="modal-backdrop"
      onClick={e => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="modal">
        <h2 id="start-new-call-title">{t("start_new_call_confirm_heading")}</h2>
        <p>{t("start_new_call_confirm_body")}</p>
        <div className="modal__actions">
          <button type="button" className="chip-btn" onClick={onCancel}>
            {t("start_new_call_confirm_cancel")}
          </button>
          <button
            ref={primaryRef}
            type="button"
            className="chip-btn chip-btn--primary"
            onClick={onConfirm}
          >
            {t("start_new_call_confirm_primary")}
          </button>
        </div>
      </div>
    </div>
  );
}
