import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("frontend package manifest", () => {
  it("does not depend on machine-local file packages outside the repository", () => {
    const packageJson = readFileSync(join(process.cwd(), "package.json"), "utf8");
    const packageLock = readFileSync(join(process.cwd(), "package-lock.json"), "utf8");

    expect(packageJson).not.toMatch(/file:\.\.\//);
    expect(packageJson).not.toContain("Web Dev");
    expect(packageLock).not.toMatch(/file:\.\.\//);
    expect(packageLock).not.toContain("Web Dev");
  });
});
