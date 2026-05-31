// frontend/tests/preflight-chat.test.tsx

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PreflightChat } from "../components/PreflightChat";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";
import type { TranscriptMessage } from "../lib/state";
import React from "react";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

const aiQuestion: TranscriptMessage = {
  id: "t-1", role: "ai_to_user", text: "几位用餐？", lang: "zh",
  is_final: true, subtype: "original", parent_id: null, segment_id: null,
  created_at: "2026-05-07T12:00:00Z",
};
const localUserAnswer = { id: "local-1", text: "4 人", ts: "2026-05-07T12:00:01Z" };

describe("<PreflightChat>", () => {
  it("renders AI questions and user-typed inputs with distinct bubble styles", () => {
    render(wrap(<PreflightChat
      transcripts={[aiQuestion]}
      localInputs={[localUserAnswer]}
    />));
    expect(screen.getByText("几位用餐？").closest(".bubble--ai")).not.toBeNull();
    // DEVIATION: use .bubble--user-supplement (existing class) instead of .bubble--user
    expect(screen.getByText("4 人").closest(".bubble--user-supplement")).not.toBeNull();
  });

  it("merges local inputs and transcripts in chronological order", () => {
    const second: TranscriptMessage = {
      id: "t-2", role: "ai_to_user", text: "您要日期？", lang: "zh",
      is_final: true, subtype: "original", parent_id: null, segment_id: null,
      created_at: "2026-05-07T12:00:02Z",
    };
    render(wrap(<PreflightChat
      transcripts={[aiQuestion, second]}
      localInputs={[localUserAnswer]}
    />));
    const bubbles = screen.getAllByText(/^(几位用餐？|4 人|您要日期？)$/);
    expect(bubbles.map(b => b.textContent)).toEqual(["几位用餐？", "4 人", "您要日期？"]);
  });

  it("auto-scrolls to the latest message on new transcript", () => {
    const scrollSpy = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollSpy;
    const { rerender } = render(wrap(<PreflightChat transcripts={[aiQuestion]} localInputs={[]} />));
    rerender(wrap(<PreflightChat transcripts={[aiQuestion]} localInputs={[localUserAnswer]} />));
    expect(scrollSpy).toHaveBeenCalled();
  });
});
