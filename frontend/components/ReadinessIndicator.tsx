import React from "react";
import { useTranslations } from "next-intl";

interface Props {
  passed: boolean;
  missing_critical: string[];
  confidence: number;
}

export function ReadinessIndicator({ passed, missing_critical }: Props) {
  const t = useTranslations("readiness");

  if (passed) {
    return (
      <div className="alert alert--ok">
        <span>{t("ready")}</span>
      </div>
    );
  }

  return (
    <div className="alert alert--warn">
      <span>{t("waiting")}</span>
      {missing_critical.length > 0 && (
        <span className="alert__detail">
          {t("missing", { fields: missing_critical.join(", ") })}
        </span>
      )}
    </div>
  );
}
