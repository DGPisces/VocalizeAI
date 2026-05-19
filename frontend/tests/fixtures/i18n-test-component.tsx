import React from "react";
import { useTranslations } from "next-intl";
export function TestComponent() {
  const t = useTranslations("supplement_input");
  return <button>{t("send")}</button>;
}
