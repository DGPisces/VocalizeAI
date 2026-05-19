import { describe, expect, it } from "vitest";
import zh from "../messages/zh.json";
import en from "../messages/en.json";

function flatten(obj: Record<string, unknown>, prefix = ""): string[] {
  const keys: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      keys.push(...flatten(v as Record<string, unknown>, path));
    } else {
      keys.push(path);
    }
  }
  return keys.sort();
}

describe("i18n key parity", () => {
  it("zh and en have the exact same key set", () => {
    expect(flatten(zh)).toEqual(flatten(en));
  });

  it("has at least the 80 keys spec §1.1 calls for", () => {
    const total = flatten(zh).length;
    expect(total).toBeGreaterThanOrEqual(80);
    expect(total).toBeLessThanOrEqual(160);
  });
});
