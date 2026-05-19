// frontend/tests/text-supplement-input.test.tsx — new

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TextSupplementInput } from "../components/TextSupplementInput";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import React from "react";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

describe("<TextSupplementInput>", () => {
  it("submits with mode=default by default", async () => {
    const onSend = vi.fn();
    render(wrap(<TextSupplementInput onSend={onSend} phase="execution_active" />));
    await userEvent.type(screen.getByRole("textbox"), "再加一位");
    await userEvent.click(screen.getByRole("button", { name: /send|发送/i }));
    expect(onSend).toHaveBeenCalledWith({
      text: "再加一位",
      lang_hint: undefined,
      mode: "default",
    });
  });

  it("renders user_takeover placeholder when mode=user_takeover", () => {
    render(wrap(<TextSupplementInput onSend={() => {}} mode="user_takeover" phase="execution_active" />));
    expect(screen.getByPlaceholderText("我来说")).toBeInTheDocument();
  });

  it("clears the input after submit", async () => {
    const onSend = vi.fn();
    render(wrap(<TextSupplementInput onSend={onSend} phase="execution_active" />));
    const input = screen.getByRole("textbox") as HTMLInputElement;
    await userEvent.type(input, "x");
    await userEvent.click(screen.getByRole("button"));
    expect(input.value).toBe("");
  });

  it("does not submit empty / whitespace-only input", async () => {
    const onSend = vi.fn();
    render(wrap(<TextSupplementInput onSend={onSend} phase="execution_active" />));
    await userEvent.click(screen.getByRole("button"));
    expect(onSend).not.toHaveBeenCalled();
    await userEvent.type(screen.getByRole("textbox"), "   ");
    await userEvent.click(screen.getByRole("button"));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("test_placeholder_resolves_for_preflight_phase", () => {
    render(wrap(<TextSupplementInput onSend={() => {}} phase="collecting" mode="default" />));
    expect(screen.getByPlaceholderText("随时补充：例如改成两个人")).toBeInTheDocument();
  });

  it("test_placeholder_resolves_for_in_call_default", () => {
    render(wrap(<TextSupplementInput onSend={() => {}} phase="execution_active" mode="default" />));
    expect(screen.getByPlaceholderText("提示 AI")).toBeInTheDocument();
  });

  it("test_placeholder_resolves_for_in_call_takeover", () => {
    render(wrap(<TextSupplementInput onSend={() => {}} phase="execution_active" mode="user_takeover" />));
    expect(screen.getByPlaceholderText("我来说")).toBeInTheDocument();
  });

  it("test_submit_sends_text_input_frame_in_preflight", async () => {
    const onSend = vi.fn();
    render(wrap(<TextSupplementInput onSend={onSend} phase="collecting" mode="default" userLang="zh" />));
    await userEvent.type(screen.getByRole("textbox"), "改成两个人");
    await userEvent.click(screen.getByRole("button", { name: /send|发送/i }));
    expect(onSend).toHaveBeenCalledWith({
      text: "改成两个人",
      lang_hint: "zh",
      mode: "default",
    });
  });
});
