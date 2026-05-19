// frontend/tests/handover-panel.test.tsx — new

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { HandoverPanel } from "../components/HandoverPanel";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

describe("<HandoverPanel>", () => {
  const steps = [
    "拿起 iPhone", "打开扬声器", "把它放在笔记本附近",
  ];

  it("renders the three physical handover steps", () => {
    render(wrap(<HandoverPanel onTakeover={() => {}} disabled={false} />));
    for (const step of steps) {
      expect(screen.getByText(new RegExp(step))).toBeInTheDocument();
    }
  });

  it("AI takeover button calls onTakeover", async () => {
    const onTakeover = vi.fn();
    render(wrap(<HandoverPanel onTakeover={onTakeover} disabled={false} />));
    await userEvent.click(screen.getByRole("button", { name: /AI 接管|AI takeover/i }));
    expect(onTakeover).toHaveBeenCalledTimes(1);
  });

  it("when disabled (readiness regressed), takeover button is disabled with tooltip", () => {
    render(wrap(<HandoverPanel onTakeover={() => {}} disabled />));
    const btn = screen.getByRole("button", { name: /AI 接管|AI takeover/i });
    expect(btn).toBeDisabled();
    expect(btn.getAttribute("title")).toMatch(/信息已变|Info changed/);
  });

  it("does not call onTakeover when disabled (defensive)", async () => {
    const onTakeover = vi.fn();
    render(wrap(<HandoverPanel onTakeover={onTakeover} disabled />));
    await userEvent.click(screen.getByRole("button", { name: /AI 接管|AI takeover/i }));
    expect(onTakeover).not.toHaveBeenCalled();
  });
});
