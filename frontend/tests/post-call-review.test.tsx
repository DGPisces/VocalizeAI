// frontend/tests/post-call-review.test.tsx — new

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PostCallReview } from "../components/PostCallReview";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import type { CallbackEntry, SlotAssumption } from "../lib/state";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

const sa: SlotAssumption = {
  id: "a-1", slot: "party_size", question: "几位？", assumed_value: 4,
  source: "user_timeout", created_at: "x", status: "pending_review",
  correction: null, note: null, callback_id: null,
};
const cb: CallbackEntry = {
  id: "cb-1", assumption_id: "a-1", correction: "6",
  note: null, status: "queued", created_at: "x",
  started_at: null, completed_at: null, transcript_segment_id: null,
};

describe("<PostCallReview>", () => {
  it("lists each uncertain_assumption with two action buttons", () => {
    render(wrap(<PostCallReview
      assumptions={[sa]}
      callbacks={[]}
      onConfirm={() => {}}
      onCorrect={() => {}}
      onTriggerCallback={() => {}}
      onCancelCallback={() => {}}
    />));
    expect(screen.getByText("party_size")).toBeInTheDocument();
    expect(screen.getByText(/4/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /确认正确/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /指出错误/i })).toBeInTheDocument();
  });

  it("clicking 指出错误 opens the inline correction form", async () => {
    render(wrap(<PostCallReview
      assumptions={[sa]} callbacks={[]}
      onConfirm={() => {}} onCorrect={() => {}}
      onTriggerCallback={() => {}} onCancelCallback={() => {}}
    />));
    await userEvent.click(screen.getByRole("button", { name: /指出错误/i }));
    expect(screen.getByLabelText(/正确值|correct value/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/备注|note/i)).toBeInTheDocument();
  });

  it("does not show review actions after an assumption is confirmed", () => {
    render(wrap(<PostCallReview
      assumptions={[{ ...sa, status: "confirmed" }]}
      callbacks={[]}
      onConfirm={() => {}} onCorrect={() => {}}
      onTriggerCallback={() => {}} onCancelCallback={() => {}}
    />));

    expect(screen.getByText("party_size")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /确认正确/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /指出错误/i })).not.toBeInTheDocument();
  });

  it("submitting the correction form fires onCorrect with assumption_id + correction + note", async () => {
    const onCorrect = vi.fn();
    render(wrap(<PostCallReview
      assumptions={[sa]} callbacks={[]}
      onConfirm={() => {}} onCorrect={onCorrect}
      onTriggerCallback={() => {}} onCancelCallback={() => {}}
    />));
    await userEvent.click(screen.getByRole("button", { name: /指出错误/i }));
    await userEvent.type(screen.getByLabelText(/正确值/i), "6");
    await userEvent.type(screen.getByLabelText(/备注/i), "actually six adults");
    await userEvent.click(screen.getByRole("button", { name: /提交|submit/i }));
    expect(onCorrect).toHaveBeenCalledWith({
      assumption_id: "a-1", correction: "6", note: "actually six adults",
    });
  });

  it("立刻拨 fires onTriggerCallback with callback id", async () => {
    const onTriggerCallback = vi.fn();
    render(wrap(<PostCallReview
      assumptions={[sa]} callbacks={[cb]}
      onConfirm={() => {}} onCorrect={() => {}}
      onTriggerCallback={onTriggerCallback} onCancelCallback={() => {}}
    />));
    await userEvent.click(screen.getByRole("button", { name: /立刻拨/i }));
    expect(onTriggerCallback).toHaveBeenCalledWith("cb-1");
  });

  it("hides callback actions after callback leaves queued status", () => {
    render(wrap(<PostCallReview
      assumptions={[]} callbacks={[{ ...cb, status: "completed" }]}
      onConfirm={() => {}} onCorrect={() => {}}
      onTriggerCallback={() => {}} onCancelCallback={() => {}}
    />));
    expect(screen.queryByRole("button", { name: /立刻拨/i })).not.toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("empty state when no uncertain assumptions", () => {
    render(wrap(<PostCallReview
      assumptions={[]} callbacks={[]}
      onConfirm={() => {}} onCorrect={() => {}}
      onTriggerCallback={() => {}} onCancelCallback={() => {}}
    />));
    expect(screen.getByText("通话顺利完成，所有信息均已确认 ✓")).toBeInTheDocument();
  });
});
