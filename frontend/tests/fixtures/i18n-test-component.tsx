import React from "react";
import { useTranslations } from "@/src/i18n";
export function TestComponent() {
  const t = useTranslations("supplement_input");
  return <button>{t("send")}</button>;
}
