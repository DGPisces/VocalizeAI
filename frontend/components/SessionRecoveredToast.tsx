import React, { useEffect } from "react";
import { useTranslations } from "@/src/i18n";

interface Props {
  onDismiss: () => void;
}

export function SessionRecoveredToast({ onDismiss }: Props) {
  const t = useTranslations("session");

  useEffect(() => {
    const timeout = window.setTimeout(onDismiss, 5000);
    return () => window.clearTimeout(timeout);
  }, [onDismiss]);

  return (
    <div className="session-recovered-toast" role="status" aria-live="polite">
      {t("recovered")}
    </div>
  );
}
