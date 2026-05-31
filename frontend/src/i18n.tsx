import React, { createContext, useContext } from "react";

import en from "../messages/en.json";
import zh from "../messages/zh.json";

type Locale = "zh" | "en";
type Messages = typeof zh;

const MESSAGE_BY_LOCALE: Record<Locale, Messages> = {
  zh,
  en: en as Messages,
};

interface I18nContextValue {
  locale: Locale;
  messages: Messages;
}

const I18nContext = createContext<I18nContextValue>({
  locale: "zh",
  messages: zh,
});

interface ProviderProps {
  children: React.ReactNode;
  locale: string;
  messages?: Messages;
}

export function I18nProvider({ children, locale, messages }: ProviderProps) {
  const normalized: Locale = locale === "en" ? "en" : "zh";
  return (
    <I18nContext.Provider
      value={{
        locale: normalized,
        messages: messages ?? MESSAGE_BY_LOCALE[normalized],
      }}
    >
      {children}
    </I18nContext.Provider>
  );
}

export function useLocale(): Locale {
  return useContext(I18nContext).locale;
}

export function useMessages(): Messages {
  return useContext(I18nContext).messages;
}

export function useTranslations(namespace?: string) {
  const { messages } = useContext(I18nContext);
  return (key: string, values?: Record<string, unknown>): string => {
    const fullKey = namespace ? `${namespace}.${key}` : key;
    const value = readPath(messages, fullKey);
    const template = typeof value === "string" ? value : fullKey;
    return interpolate(template, values);
  };
}

function readPath(root: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((current, segment) => {
    if (current && typeof current === "object" && segment in current) {
      return (current as Record<string, unknown>)[segment];
    }
    return undefined;
  }, root);
}

function interpolate(
  template: string,
  values: Record<string, unknown> | undefined,
): string {
  if (!values) {
    return template;
  }
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    const value = values[name.trim()];
    return value === undefined || value === null ? "" : String(value);
  });
}
