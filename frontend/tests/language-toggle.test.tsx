// frontend/tests/language-toggle.test.tsx

import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LanguageToggle } from "../components/LanguageToggle";

// Mock i18n helper's useLocale
vi.mock("@/src/i18n", () => ({
  useLocale: vi.fn(() => "zh"),
}));

// Mock router helper
const mockReplace = vi.fn();
const mockSearchParams = vi.fn(() => new URLSearchParams());
vi.mock("@/src/router", () => ({
  useRouter: () => ({ replace: mockReplace }),
  usePathname: vi.fn(() => "/zh"),
  useSearchParams: () => mockSearchParams(),
}));

import { useLocale } from "@/src/i18n";
import { usePathname } from "@/src/router";

const mockUseLocale = vi.mocked(useLocale);
const mockUsePathname = vi.mocked(usePathname);

describe("<LanguageToggle>", () => {
  beforeEach(() => {
    mockReplace.mockClear();
    mockUseLocale.mockReturnValue("zh");
    mockUsePathname.mockReturnValue("/zh");
    mockSearchParams.mockReturnValue(new URLSearchParams());
    localStorage.clear();
  });

  it("renders 中 when locale is zh", () => {
    mockUseLocale.mockReturnValue("zh");
    render(<LanguageToggle />);
    expect(screen.getByRole("button").textContent).toBe("中");
    expect(screen.getByRole("button")).toHaveAccessibleName("Switch to English");
  });

  it("renders EN when locale is en", () => {
    mockUseLocale.mockReturnValue("en");
    mockUsePathname.mockReturnValue("/en");
    render(<LanguageToggle />);
    expect(screen.getByRole("button").textContent).toBe("EN");
    expect(screen.getByRole("button")).toHaveAccessibleName("切换到中文");
  });

  it("click when zh stores en and keeps the live session path plus query string", async () => {
    mockUseLocale.mockReturnValue("zh");
    mockUsePathname.mockReturnValue("/zh/live/s-1");
    mockSearchParams.mockReturnValue(new URLSearchParams("ws=ws://example.test/ws/sessions/s-1&debug=1"));
    render(<LanguageToggle />);
    await userEvent.click(screen.getByRole("button"));
    expect(mockReplace).toHaveBeenCalledWith("/en/live/s-1?ws=ws%3A%2F%2Fexample.test%2Fws%2Fsessions%2Fs-1&debug=1");
    expect(localStorage.getItem("preferred_ui_lang")).toBe("en");
  });

  it("click when en stores zh and keeps the live session path", async () => {
    mockUseLocale.mockReturnValue("en");
    mockUsePathname.mockReturnValue("/en/live/s-1");
    render(<LanguageToggle />);
    await userEvent.click(screen.getByRole("button"));
    expect(mockReplace).toHaveBeenCalledWith("/zh/live/s-1");
    expect(localStorage.getItem("preferred_ui_lang")).toBe("zh");
  });
});
