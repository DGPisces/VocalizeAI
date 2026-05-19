// frontend/tests/og-cards.test.ts

import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";

const PUBLIC_DIR = path.resolve(__dirname, "../public/og");

describe("OG share cards", () => {
  it("og-zh.png exists with reasonable size", () => {
    const p = path.join(PUBLIC_DIR, "og-zh.png");
    expect(fs.existsSync(p)).toBe(true);
    const size = fs.statSync(p).size;
    expect(size).toBeGreaterThan(20_000);
    expect(size).toBeLessThan(220_000);
  });

  it("og-en.png exists with reasonable size", () => {
    const p = path.join(PUBLIC_DIR, "og-en.png");
    expect(fs.existsSync(p)).toBe(true);
    const size = fs.statSync(p).size;
    expect(size).toBeGreaterThan(20_000);
    expect(size).toBeLessThan(220_000);
  });
});
