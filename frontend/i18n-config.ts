export const SUPPORTED_LOCALES = ["zh", "en"] as const;
export const DEFAULT_LOCALE = "zh";
export type Locale = (typeof SUPPORTED_LOCALES)[number];
