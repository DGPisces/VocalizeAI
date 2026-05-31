/**
 * Task 0.2 — Design-system import + component CSS classes (smoke test).
 *
 * Plan-spec rationale (deviation from verbatim plan):
 *   The plan's verbatim test imports `<HangupButton>` from `@/components/HangupButton`,
 *   but that component is built in Task C4 (much later) and the project's tsconfig has
 *   no `compilerOptions.paths` entry — so importing a not-yet-existent component via a
 *   not-yet-configured alias would fail at compile time. The real intent of Step 1 is:
 *   "smoke check that key classes are reachable from rendered components" → in other
 *   words, that the component-class catalogue this task ships matches the catalogue
 *   the plan lists. We test that catalogue contract directly by reading components.css
 *   and asserting every selector the plan promises is present. This (a) decouples
 *   Task 0.2 from Task C4's existence, (b) survives as a contract test after C4 lands,
 *   and (c) gives a stronger guarantee than a single-component class assertion.
 */

import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const COMPONENTS_CSS_PATH = resolve(__dirname, "..", "app", "components.css");
const GLOBALS_CSS_PATH = resolve(__dirname, "..", "app", "globals.css");

/**
 * The full catalogue of component classes from
 * `docs/archive/superpowers/plans/2026-05-07-plan-b3a-ui.md` Task 0.2 Step 3.
 * Phase C/D components depend on every name here being styled.
 */
const REQUIRED_SELECTORS: readonly string[] = [
  // Bubbles
  ".bubble",
  ".bubble--ai",
  ".bubble--ai-to-merchant",
  ".bubble--merchant-to-ai",
  ".bubble--user-supplement",
  ".bubble--user-takeover",
  ".bubble--callback",
  ".bubble--system",
  ".bubble__label",
  ".bubble__text",
  ".bubble__translation",
  ".skeleton-text",
  ".callback-separator",
  // Chips / buttons
  ".chip-btn",
  ".chip-btn--primary",
  ".chip-btn--danger",
  // Modal / overlay
  ".modal-backdrop",
  ".modal",
  ".modal__actions",
  // Alerts
  ".alert",
  ".alert--ok",
  ".alert--warn",
  ".alert__detail",
  // Progress
  ".progress",
  ".progress__fill",
  // Specific surfaces
  ".preflight-chat",
  ".transcript-stream",
  ".clarification-toast",
  ".clarification-toast__question",
  ".clarification-toast__bar",
  ".clarification-toast__countdown",
  ".clarification-modal-backdrop",
  ".clarification-modal",
  ".clarification-modal__countdown",
  ".post-call-review",
  ".post-call-review--empty",
  ".assumptions",
  ".callbacks",
  ".assumption-row",
  ".callback-row",
  ".assumption-row__actions",
  ".assumption-row__form",
  ".callback-row__summary",
  ".callback-row__actions",
  ".text-supplement-input"
];

describe("styling smoke (Task 0.2)", () => {
  const css = readFileSync(COMPONENTS_CSS_PATH, "utf8");

  it("components.css exists and is non-empty", () => {
    expect(css.length).toBeGreaterThan(0);
  });

  it.each(REQUIRED_SELECTORS)(
    "components.css defines selector %s",
    selector => {
      // Match the selector at a token boundary: it must appear followed by
      // a non-identifier character (`{`, `,`, ` `, `:`, `>`, etc.) so that
      // e.g. `.bubble` is not accidentally satisfied by `.bubble__text`.
      const escaped = selector.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
      const pattern = new RegExp(`${escaped}(?![A-Za-z0-9_-])`);
      expect(css).toMatch(pattern);
    }
  );

  it("each required selector has a non-empty rule body", () => {
    // Ensure no class is shipped as a `.foo { }` placeholder. Find every
    // top-level rule for a required selector and check the body is non-trivial
    // (contains at least one declaration `prop: value`).
    const missingBodies: string[] = [];
    for (const selector of REQUIRED_SELECTORS) {
      const escaped = selector.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
      // Selector token, optional more selectors, then `{ ... }`.
      const rulePattern = new RegExp(
        `${escaped}(?![A-Za-z0-9_-])[^{]*\\{([^}]*)\\}`
      );
      const match = css.match(rulePattern);
      if (!match) {
        missingBodies.push(`${selector}: no rule found`);
        continue;
      }
      const body = match[1].trim();
      if (!/[a-z-]+\s*:\s*\S/i.test(body)) {
        missingBodies.push(`${selector}: rule body has no declarations`);
      }
    }
    expect(missingBodies).toEqual([]);
  });
});

describe("dark mode tokens", () => {
  const globalsCss = readFileSync(GLOBALS_CSS_PATH, "utf8");
  const componentsCss = readFileSync(COMPONENTS_CSS_PATH, "utf8");

  it("globals.css supports explicit and system dark mode", () => {
    expect(globalsCss).toContain(':root[data-theme="dark"]');
    expect(globalsCss).toContain("@media (prefers-color-scheme: dark)");
    expect(globalsCss).toContain(':root:not([data-theme="light"]):not([data-theme="dark"])');
    expect(globalsCss).toContain("color-scheme: dark");
  });

  it("hard-coded light surfaces use dark-aware variables", () => {
    expect(globalsCss).toContain("--readiness-bg");
    expect(globalsCss).toContain("background: var(--readiness-bg)");
    expect(globalsCss).toContain("--assistant-row-bg");
    expect(globalsCss).toContain("background: var(--assistant-row-bg)");
  });

  it("component shimmer respects system dark mode", () => {
    expect(componentsCss).toContain("@media (prefers-color-scheme: dark)");
    expect(componentsCss).toContain(':root:not([data-theme="light"]):not([data-theme="dark"]) .skeleton-text::after');
  });
});
