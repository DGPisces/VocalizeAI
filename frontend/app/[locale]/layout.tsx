import type { Metadata } from "next";
import { NextIntlClientProvider, useMessages } from "next-intl";
import { unstable_setRequestLocale } from "next-intl/server";
import "../globals.css";
import "../components.css";
import { SUPPORTED_LOCALES } from "../../i18n";

export function generateStaticParams() {
  return SUPPORTED_LOCALES.map(locale => ({ locale }));
}

interface Props {
  children: React.ReactNode;
  params: { locale: string };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const ogImage = params.locale === "en" ? "/og/og-en.png" : "/og/og-zh.png";
  const description =
    params.locale === "en"
      ? "Browser audio bridge for AI phone tasks"
      : "AI 电话助手的浏览器音频桥";
  return {
    title: "VocalizeAI",
    description,
    openGraph: { images: [{ url: ogImage, width: 1200, height: 630 }] },
  };
}

export default function LocaleLayout({ children, params }: Props) {
  unstable_setRequestLocale(params.locale);
  const messages = useMessages();
  return (
    <html lang={params.locale} suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t)}}catch(e){}`
          }}
        />
      </head>
      <body>
        <a className="skip-link" href="#main">Skip to content</a>
        <NextIntlClientProvider locale={params.locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
