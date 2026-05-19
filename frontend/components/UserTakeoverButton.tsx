// frontend/components/UserTakeoverButton.tsx — new

import React from "react";
import { useTranslations } from "next-intl";

interface Props {
  active: boolean;
  onToggle: (next: boolean) => void;
}

export function UserTakeoverButton({ active, onToggle }: Props) {
  const t = useTranslations("user_takeover");
  return (
    <button
      type="button"
      className={`chip-btn ${active ? "chip-btn--primary" : ""}`}
      aria-pressed={active}
      onClick={() => onToggle(!active)}
    >
      {active ? t("active") : t("inactive")}
    </button>
  );
}
