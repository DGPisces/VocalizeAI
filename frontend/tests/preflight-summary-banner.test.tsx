// frontend/tests/preflight-summary-banner.test.tsx

import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PreflightSummaryBanner } from "../components/PreflightSummaryBanner";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import type { TranscriptMessage } from "../lib/state";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

const makeMsg = (id: string, role: TranscriptMessage["role"], text: string): TranscriptMessage => ({
  id,
  role,
  text,
  lang: "zh",
  is_final: true,
  subtype: "original",
  parent_id: null,
  segment_id: null,
  created_at: "2026-01-01T00:00:00Z",
});

describe("<PreflightSummaryBanner>", () => {
  it("renders single-line summary from slots", () => {
    const slots = { party: "4 人", time: "今晚 7 点", restaurant: "北京餐厅" };
    render(wrap(<PreflightSummaryBanner slots={slots} preflight_history={[]} />));
    const toggle = screen.getByRole("button");
    expect(toggle.textContent).toContain("4 人");
    expect(toggle.textContent).toContain("今晚 7 点");
    expect(toggle.textContent).toContain("北京餐厅");
  });

  it("click expands and shows preflight history items", async () => {
    const slots = { party: "4 人" };
    const history = [
      makeMsg("m1", "ai_to_user", "请问几位？"),
      makeMsg("m2", "user_supplement", "4个人"),
    ];
    render(wrap(<PreflightSummaryBanner slots={slots} preflight_history={history} />));
    const toggle = screen.getByRole("button");
    // history should not be visible yet
    expect(screen.queryByText("请问几位？", { exact: false })).toBeNull();
    await userEvent.click(toggle);
    expect(screen.getByText("请问几位？", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("4个人", { exact: false })).toBeInTheDocument();
  });

  it("click again collapses history", async () => {
    const slots = { party: "4 人" };
    const history = [makeMsg("m1", "ai_to_user", "几位？")];
    render(wrap(<PreflightSummaryBanner slots={slots} preflight_history={history} />));
    const toggle = screen.getByRole("button");
    await userEvent.click(toggle);
    expect(screen.getByText("几位？", { exact: false })).toBeInTheDocument();
    await userEvent.click(toggle);
    expect(screen.queryByText("几位？", { exact: false })).toBeNull();
  });
});
