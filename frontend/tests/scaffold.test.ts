import { describe, expect, it } from "vitest";

describe("frontend scaffold", () => {
  it("loads the test environment", () => {
    expect(document.createElement("main")).toBeInstanceOf(HTMLElement);
  });
});
