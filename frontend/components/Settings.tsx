import React, { useEffect } from "react";
import { useTranslations } from "@/src/i18n";
import { DeviceSettings } from "./DeviceSettings";
import type { DevicePreferences, DeviceSwitchStatus } from "./DeviceSettings";

interface Props {
  open: boolean;
  onClose: () => void;
  // DeviceSettings dependencies passed through:
  autoTranslate: boolean;
  onAutoTranslateChange: (next: boolean) => void;
  devicePreferences?: DevicePreferences;
  onDevicePreferencesChange?: (preferences: DevicePreferences) => void;
  deviceSwitchStatus?: DeviceSwitchStatus | null;
}

export function Settings({
  open,
  onClose,
  autoTranslate,
  onAutoTranslateChange,
  devicePreferences,
  onDevicePreferencesChange,
  deviceSwitchStatus,
}: Props) {
  const t = useTranslations("settings");

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      className="settings-backdrop"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <aside className="settings-sheet">
        <header className="settings-sheet__header">
          <h2 id="settings-title">{t("title")}</h2>
          <button type="button" className="chip-btn" onClick={onClose} aria-label={t("close")}>
            ×
          </button>
        </header>
        <DeviceSettings
          autoTranslate={autoTranslate}
          onAutoTranslateChange={onAutoTranslateChange}
          devicePreferences={devicePreferences}
          onDevicePreferencesChange={onDevicePreferencesChange}
          deviceSwitchStatus={deviceSwitchStatus}
        />
      </aside>
    </div>
  );
}
