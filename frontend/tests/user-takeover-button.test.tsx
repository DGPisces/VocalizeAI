// frontend/tests/user-takeover-button.test.tsx — new

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UserTakeoverButton } from "../components/UserTakeoverButton";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

describe("<UserTakeoverButton>", () => {
  it("clicking when off calls onToggle(true)", async () => {
    const onToggle = vi.fn();
    render(wrap(<UserTakeoverButton active={false} onToggle={onToggle} />));
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("clicking when on calls onToggle(false)", async () => {
    const onToggle = vi.fn();
    render(wrap(<UserTakeoverButton active={true} onToggle={onToggle} />));
    expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledWith(false);
  });

  it("double-click triggers two onToggle calls (back to off from parent perspective)", async () => {
    const calls: boolean[] = [];
    const onToggle = vi.fn((v: boolean) => calls.push(v));
    render(wrap(<UserTakeoverButton active={false} onToggle={onToggle} />));
    await userEvent.dblClick(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledTimes(2);
    // Both clicks receive the same prop (false), so both calls are onToggle(true)
    expect(calls).toEqual([true, true]);
  });
});
