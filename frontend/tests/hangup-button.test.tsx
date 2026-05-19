// frontend/tests/hangup-button.test.tsx — new

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HangupButton } from "../components/HangupButton";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

describe("<HangupButton>", () => {
  it("clicking the button opens the confirm modal", async () => {
    render(wrap(<HangupButton onConfirm={() => {}} />));
    expect(screen.queryByRole("dialog")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /挂断/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("确定挂断这通电话吗？")).toBeInTheDocument();
  });

  it("confirming fires onConfirm exactly once and closes modal", async () => {
    const onConfirm = vi.fn();
    render(wrap(<HangupButton onConfirm={onConfirm} />));
    await userEvent.click(screen.getByRole("button", { name: /挂断/i }));
    await userEvent.click(screen.getByRole("button", { name: /确认挂断/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancelling closes modal without firing onConfirm", async () => {
    const onConfirm = vi.fn();
    render(wrap(<HangupButton onConfirm={onConfirm} />));
    await userEvent.click(screen.getByRole("button", { name: /挂断/i }));
    await userEvent.click(screen.getByRole("button", { name: /取消/i }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
