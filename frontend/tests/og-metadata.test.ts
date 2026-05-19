// frontend/tests/og-metadata.test.ts
//
// Unit-tests that generateMetadata() in [locale]/layout.tsx routes the
// correct OG image URL per locale.
//
// next-intl/server calls (unstable_setRequestLocale, useMessages) run only
// in the default export component, NOT in generateMetadata. We mock the
// next-intl modules so the import succeeds in the vitest/jsdom environment.

import { describe, expect, it, vi } from "vitest";

// Mock next-intl before importing the module under test
vi.mock("next-intl", () => ({
  NextIntlClientProvider: () => null,
  useMessages: () => ({}),
}));

vi.mock("next-intl/server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("next-intl/server")>();
  return {
    ...actual,
    unstable_setRequestLocale: () => undefined,
  };
});

// Dynamic import runs after vi.mock() hoisting is applied
const { generateMetadata } = await import("../app/[locale]/layout");

describe("OG metadata", () => {
  it("produces an OG image entry per locale", async () => {
    const metaZh = await generateMetadata({ params: { locale: "zh" }, children: null });
    expect((metaZh.openGraph?.images as any)?.[0]?.url).toContain("og-zh.png");

    const metaEn = await generateMetadata({ params: { locale: "en" }, children: null });
    expect((metaEn.openGraph?.images as any)?.[0]?.url).toContain("og-en.png");
  });
});
