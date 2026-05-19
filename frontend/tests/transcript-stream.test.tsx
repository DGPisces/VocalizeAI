// frontend/tests/transcript-stream.test.tsx — new

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptStream } from "../components/TranscriptStream";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import type { TranscriptMessage } from "../lib/state";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

const merchantOriginal: TranscriptMessage = {
  id: "t-1", role: "merchant_to_ai", text: "Hello", lang: "en",
  is_final: true, subtype: "original", parent_id: null, segment_id: null,
  created_at: "x",
};
const merchantTranslation: TranscriptMessage = {
  id: "t-2", role: "ai_to_user", text: "你好", lang: "zh",
  is_final: true, subtype: "translation", parent_id: "t-1", segment_id: null,
  created_at: "x",
};
const userSupplement: TranscriptMessage = {
  id: "t-3", role: "user_supplement", text: "他们要 6 位", lang: "zh",
  is_final: true, subtype: "user_supplement", parent_id: null, segment_id: null,
  created_at: "x",
};
const callbackSeg: TranscriptMessage = {
  id: "t-4", role: "ai_to_merchant", text: "刚才说错了一点", lang: "zh",
  is_final: true, subtype: "callback_segment", parent_id: null, segment_id: "seg-1",
  created_at: "x",
};

describe("<TranscriptStream>", () => {
  it("test_transcript_stream_renders_filler_chip_when_ai_status_filler", () => {
    const { container } = render(wrap(
      <TranscriptStream transcripts={[]} aiStatus="filler" />,
    ));
    expect(container.querySelector(".ai-status-chip")).not.toBeNull();
    expect(container.querySelector(".ai-status-chip--warn")).not.toBeNull();
    expect(screen.getByText(zh.ai_status.filler_active)).toBeInTheDocument();
  });

  it("test_transcript_stream_renders_escalation_chip_when_ai_status_escalation", () => {
    const { container } = render(wrap(
      <TranscriptStream transcripts={[]} aiStatus="escalation" />,
    ));
    expect(container.querySelector(".ai-status-chip")).not.toBeNull();
    expect(container.querySelector(".ai-status-chip--bad")).not.toBeNull();
    expect(screen.getByText(zh.ai_status.escalation_warning)).toBeInTheDocument();
  });

  it("test_transcript_stream_no_chip_when_ai_status_null", () => {
    const { container } = render(wrap(
      <TranscriptStream transcripts={[]} aiStatus={null} />,
    ));
    expect(container.querySelector(".ai-status-chip")).toBeNull();
  });

  it("renders cross-lingual translation sub-line under the original", () => {
    render(wrap(<TranscriptStream
      transcripts={[merchantOriginal, merchantTranslation]}
    />));
    const original = screen.getByText("Hello").closest(".bubble");
    expect(original?.querySelector(".bubble__translation")?.textContent).toBe("你好");
  });

  it("renders user_supplement as muted center bubble with 用户提示 AI label", () => {
    render(wrap(<TranscriptStream transcripts={[userSupplement]} />));
    expect(screen.getByText("用户提示 AI")).toBeInTheDocument();
  });

  it("renders callback_segment with separator + 回拨通话 label", () => {
    render(wrap(<TranscriptStream transcripts={[callbackSeg]} />));
    expect(screen.getByText("回拨通话")).toBeInTheDocument();
    expect(screen.getByText("刚才说错了一点").closest(".bubble--callback")).not.toBeNull();
  });

  it("hides state events by default; shows when ?debug=1", () => {
    const stateEvent: TranscriptMessage = {
      id: "t-5", role: "system", text: "DEBUG state_update",
      lang: null, is_final: true, subtype: "original", parent_id: null,
      segment_id: null, created_at: "x",
    };
    const { rerender } = render(wrap(<TranscriptStream transcripts={[stateEvent]} debug={false} />));
    expect(screen.queryByText("DEBUG state_update")).toBeNull();
    rerender(wrap(<TranscriptStream transcripts={[stateEvent]} debug />));
    expect(screen.getByText("DEBUG state_update")).toBeInTheDocument();
  });

  it("shows .skeleton-text loading flash when translation is pending (parent without translation child yet)", () => {
    render(wrap(<TranscriptStream transcripts={[merchantOriginal]} translationsPending={["t-1"]} />));
    expect(screen.getByText("Hello").closest(".bubble")?.querySelector(".skeleton-text")).not.toBeNull();
  });

  // D8: on-demand translate button tests

  it("[D8] shows 译 button on merchant bubble when autoTranslate=false and langs differ", () => {
    render(wrap(
      <TranscriptStream
        transcripts={[merchantOriginal]}
        autoTranslate={false}
        userLang="zh"
        merchantLang="en"
        onDemandTranslate={() => {}}
      />
    ));
    expect(screen.getByRole("button", { name: "译" })).toBeInTheDocument();
  });

  it("[D8] shows 译 button when merchant language is auto but the row has a language", () => {
    render(wrap(
      <TranscriptStream
        transcripts={[merchantOriginal]}
        autoTranslate={false}
        userLang="zh"
        onDemandTranslate={() => {}}
      />
    ));
    expect(screen.getByRole("button", { name: "译" })).toBeInTheDocument();
  });

  it("[D8] does NOT show 译 button when autoTranslate is true", () => {
    render(wrap(
      <TranscriptStream
        transcripts={[merchantOriginal]}
        autoTranslate={true}
        userLang="zh"
        merchantLang="en"
        onDemandTranslate={() => {}}
      />
    ));
    expect(screen.queryByRole("button", { name: "译" })).toBeNull();
  });

  it("[D8] does NOT show 译 button when translation already arrived", () => {
    render(wrap(
      <TranscriptStream
        transcripts={[merchantOriginal, merchantTranslation]}
        autoTranslate={false}
        userLang="zh"
        merchantLang="en"
        onDemandTranslate={() => {}}
      />
    ));
    expect(screen.queryByRole("button", { name: "译" })).toBeNull();
  });

  it("[D8] clicking 译 button calls onDemandTranslate with the message id", async () => {
    const onDemandTranslate = vi.fn();
    render(wrap(
      <TranscriptStream
        transcripts={[merchantOriginal]}
        autoTranslate={false}
        userLang="zh"
        merchantLang="en"
        onDemandTranslate={onDemandTranslate}
      />
    ));
    await userEvent.click(screen.getByRole("button", { name: "译" }));
    expect(onDemandTranslate).toHaveBeenCalledWith("t-1");
    expect(onDemandTranslate).toHaveBeenCalledTimes(1);
  });
});
