import React, { useState } from "react";
import { useTranslations } from "@/src/i18n";
import type { TranscriptMessage } from "../lib/state";

interface Props {
  slots: Record<string, unknown>;
  preflight_history: TranscriptMessage[];
}

export function PreflightSummaryBanner({ slots, preflight_history }: Props) {
  const t = useTranslations("preflight_summary");
  const [expanded, setExpanded] = useState(false);

  const summary = Object.values(slots)
    .filter(v => v != null && v !== "")
    .map(v => String(v))
    .join(" · ");

  return (
    <section className="card preflight-summary-banner">
      <button
        type="button"
        className="preflight-summary-banner__toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded(e => !e)}
      >
        {summary || t("no_summary")}
      </button>
      {expanded && (
        <ol className="preflight-summary-banner__history">
          {preflight_history.map(m => (
            <li key={m.id}>
              <span className="preflight-summary-banner__role">
                {m.role === "ai_to_user" ? t("ai") : t("user")}
              </span>
              {": "}
              {m.text}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
