// frontend/components/HangupButton.tsx — new

import React, { useState } from "react";
import { useTranslations } from "@/src/i18n";
import { PhoneOff } from "lucide-react";

interface Props {
  onConfirm: () => void;
}

export function HangupButton({ onConfirm }: Props) {
  const t = useTranslations("hangup");
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="chip-btn chip-btn--danger" onClick={() => setOpen(true)}>
        <PhoneOff size={16} /> {t("button")}
      </button>
      {open && (
        <div
          role="dialog"
          aria-modal={true}
          aria-labelledby="hangup-dialog-title"
          className="modal-backdrop"
          onClick={e => {
            if (e.target === e.currentTarget) setOpen(false);
          }}
        >
          <div className="modal">
            <h2 id="hangup-dialog-title">{t("confirm_question")}</h2>
            <div className="modal__actions">
              <button className="chip-btn" onClick={() => setOpen(false)}>{t("cancel")}</button>
              <button
                className="chip-btn chip-btn--danger"
                onClick={() => { setOpen(false); onConfirm(); }}
              >
                {t("confirm")}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
