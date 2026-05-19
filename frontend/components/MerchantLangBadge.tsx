import React, { useState } from "react";
import { useTranslations } from "next-intl";

type MerchantLang = "zh" | "en" | "auto";

interface Props {
  value: MerchantLang;
  onChange: (next: MerchantLang) => void;
}

export function MerchantLangBadge({ value, onChange }: Props) {
  const t = useTranslations("merchant_lang");
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<MerchantLang>(value);

  const labels: Record<MerchantLang, string> = {
    zh: t("zh"),
    en: t("en"),
    auto: t("auto"),
  };

  return (
    <div className="merchant-lang-badge">
      <button
        type="button"
        className="chip"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          setPending(value);
          setOpen(o => !o);
        }}
      >
        {t("label")}: {labels[value]}
      </button>
      {open && (
        <div role="dialog" className="merchant-lang-badge__popover">
          {(["zh", "en", "auto"] as const).map(opt => (
            <label key={opt}>
              <input
                type="radio"
                name="merchant-lang"
                value={opt}
                checked={pending === opt}
                onChange={() => setPending(opt)}
              />
              {labels[opt]}
            </label>
          ))}
          <button
            type="button"
            className="chip-btn chip-btn--primary"
            onClick={() => {
              onChange(pending);
              setOpen(false);
            }}
          >
            {t("save")}
          </button>
        </div>
      )}
    </div>
  );
}
