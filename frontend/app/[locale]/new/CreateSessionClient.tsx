"use client";

import React, { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { createSession } from "../../../lib/api";

interface Props {
  onCreated?: (sessionId: string) => void;
}

const UI_LANG_STORAGE_KEY = "preferred_ui_lang";

function readPreferredUiLocale(fallback: string): "zh" | "en" {
  const stored = localStorage.getItem(UI_LANG_STORAGE_KEY);
  if (stored === "zh" || stored === "en") {
    return stored;
  }
  return fallback === "en" ? "en" : "zh";
}

/**
 * Reads localStorage preferences, POSTs to /api/sessions, then redirects to the
 * live session page. A useRef guard prevents double-creation in React Strict Mode.
 *
 */
export function CreateSessionClient({ onCreated }: Props) {
  const router = useRouter();
  const t = useTranslations("create_session");
  const locale = useLocale();
  const fired = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (fired.current) return;
    fired.current = true;
    setError(null);

    const stored = localStorage.getItem("auto_translate_merchant");
    const auto = stored === null ? true : stored !== "false";
    const voiceIdRaw = localStorage.getItem("preferred_voice_id");
    const voiceId = voiceIdRaw !== null ? voiceIdRaw : undefined;
    const routeLocale = readPreferredUiLocale(locale);

    createSession({
      auto_translate_merchant: auto,
      preferred_voice_id: voiceId,
      default_lang: locale === "en" ? "en" : "zh",
    })
      .then(s => {
        onCreated?.(s.session_id);
        router.replace(`/${routeLocale}/live/${s.session_id}?ws=${encodeURIComponent(s.ws_url)}`);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : String(err));
      });
  }, [attempt, locale, router, onCreated]);

  if (error) {
    return (
      <div>
        <p role="alert">{t("error", { message: error })}</p>
        <button
          type="button"
          onClick={() => {
            fired.current = false;
            setError(null);
            setAttempt((value) => value + 1);
          }}
        >
          {t("retry")}
        </button>
      </div>
    );
  }

  return <p role="status" aria-live="polite">{t("status")}</p>;
}
