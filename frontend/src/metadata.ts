export function getOpenGraphImage(locale: string): string {
  return locale === "en" ? "/og/og-en.png" : "/og/og-zh.png";
}

export function getPageMetadata(locale: string) {
  return {
    title: "VocalizeAI",
    description: "Local speech and LLM task runner",
    openGraph: {
      images: [{ url: getOpenGraphImage(locale) }],
    },
  };
}
