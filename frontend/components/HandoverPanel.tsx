// frontend/components/HandoverPanel.tsx — new

import React from "react";
import { useTranslations } from "@/src/i18n";

interface Props {
  onTakeover: () => void;
  disabled: boolean;
  busy?: boolean;
  error?: string | null;
}

export function HandoverPanel({ onTakeover, disabled, busy = false, error = null }: Props) {
  const t = useTranslations("handover");
  const blocked = disabled || busy;
  return (
    <div role="dialog" aria-modal="true" className="modal-backdrop">
      <div className="modal handover-panel">
        <h2>{t("title")}</h2>
        <ol>
          <li>1. {t("step_pickup")}</li>
          <li>2. {t("step_speakerphone")}</li>
          <li>3. {t("step_place_near_laptop")}</li>
        </ol>
        <button
          className="chip-btn chip-btn--primary"
          disabled={blocked}
          title={disabled ? t("disabled_tooltip") : undefined}
          aria-busy={busy || undefined}
          onClick={onTakeover}
        >
          {busy ? t("checking_mic") : t("takeover_button")}
        </button>
        {error ? (
          <div className="alert alert--bad" role="alert">
            {error}
          </div>
        ) : null}
      </div>
    </div>
  );
}
