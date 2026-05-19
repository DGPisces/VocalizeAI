"use client";

import React from "react";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

interface Props {
  state: "connected" | "reconnecting" | "disconnected";
}

export function ConnectionStateChip({ state }: Props) {
  const t = useTranslations("errors");

  if (state === "connected") {
    return null;
  }

  return (
    <span
      className={`chip connection-state-chip${state === "disconnected" ? " chip--bad" : ""}`}
      role="status"
      aria-live={state === "disconnected" ? "assertive" : "polite"}
    >
      <Loader2
        size={12}
        aria-hidden="true"
        className={state === "reconnecting" ? "spin" : undefined}
      />
      {t("ws_disconnect")}
    </span>
  );
}
