// frontend/components/LanguageToggle.tsx

import React from "react";
import { useLocale } from "@/src/i18n";
import { useRouter, usePathname, useSearchParams } from "@/src/router";

const STORAGE_KEY = "preferred_ui_lang";

export function LanguageToggle() {
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function toggle() {
    const next = locale === "zh" ? "en" : "zh";
    try { localStorage.setItem(STORAGE_KEY, next); } catch { /* ignore */ }
    // Replace the leading /[locale] segment in the pathname.
    const newPath = pathname.replace(/^\/[a-z]{2}(\/|$)/, `/${next}$1`);
    const query = searchParams?.toString();
    router.replace(query ? `${newPath}?${query}` : newPath);
  }

  return (
    <button
      type="button"
      className="chip language-toggle"
      onClick={toggle}
      aria-label={locale === "zh" ? "Switch to English" : "切换到中文"}
    >
      {locale === "zh" ? "中" : "EN"}
    </button>
  );
}
