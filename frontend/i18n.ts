import { getRequestConfig } from "next-intl/server";
import { notFound } from "next/navigation";
import { DEFAULT_LOCALE, SUPPORTED_LOCALES, type Locale } from "./i18n-config";

export { DEFAULT_LOCALE, SUPPORTED_LOCALES, type Locale };

export default getRequestConfig(async ({ requestLocale }) => {
  const locale = await requestLocale;
  if (!locale || !SUPPORTED_LOCALES.includes(locale as Locale)) notFound();
  return {
    locale,
    messages: (await import(`./messages/${locale}.json`)).default,
  };
});
