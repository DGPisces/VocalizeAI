// frontend/tests/og-metadata.test.ts
//
// Unit-tests that page metadata routes the correct OG image URL per locale.

import { describe, expect, it } from "vitest";
import { getPageMetadata } from "../src/metadata";

describe("OG metadata", () => {
  it("produces an OG image entry per locale", async () => {
    const metaZh = getPageMetadata("zh");
    expect((metaZh.openGraph?.images as any)?.[0]?.url).toContain("og-zh.png");

    const metaEn = getPageMetadata("en");
    expect((metaEn.openGraph?.images as any)?.[0]?.url).toContain("og-en.png");
  });
});
