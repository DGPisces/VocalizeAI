// frontend/tests/merchant-lang-badge.test.tsx

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MerchantLangBadge } from "../components/MerchantLangBadge";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

describe("<MerchantLangBadge>", () => {
  it("renders chip with current value label", () => {
    render(wrap(<MerchantLangBadge value="zh" onChange={() => {}} />));
    expect(screen.getByRole("button", { name: /商家语言.*中文/i })).toBeInTheDocument();
  });

  it("clicking chip opens popover with 3 options", async () => {
    render(wrap(<MerchantLangBadge value="zh" onChange={() => {}} />));
    await userEvent.click(screen.getByRole("button", { name: /商家语言/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // 3 radio options
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3);
  });

  it("select option + click save fires onChange and closes popover", async () => {
    const onChange = vi.fn();
    render(wrap(<MerchantLangBadge value="zh" onChange={onChange} />));
    await userEvent.click(screen.getByRole("button", { name: /商家语言/i }));
    // select "en" radio
    await userEvent.click(screen.getByRole("radio", { name: /English/i }));
    // click save
    await userEvent.click(screen.getByRole("button", { name: /保存/i }));
    expect(onChange).toHaveBeenCalledWith("en");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("clicking chip again without saving closes popover without firing onChange", async () => {
    const onChange = vi.fn();
    render(wrap(<MerchantLangBadge value="zh" onChange={onChange} />));
    const chip = screen.getByRole("button", { name: /商家语言/i });
    // open
    await userEvent.click(chip);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // close without saving
    await userEvent.click(chip);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });
});
