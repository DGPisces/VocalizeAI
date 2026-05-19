import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import { MockWebSocket } from "./setup";
import { ReviewPageClient, type ReviewApiClient } from "../app/[locale]/review/[session]/ReviewPageClient";
import type { GetReviewResponse } from "../lib/api";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

function review(overrides: Partial<GetReviewResponse> = {}): GetReviewResponse {
  return {
    session_id: "sess-1",
    status: "completed",
    slots: {},
    uncertain_assumptions: [{
      id: "a-1",
      slot: "restaurant",
      question: "Which one?",
      assumed_value: "A",
      source: "user_timeout",
      created_at: "2026-05-07T12:00:00Z",
      status: "pending_review",
      correction: null,
      note: null,
      callback_id: null,
    }],
    pending_callbacks: [{
      id: "cb-1",
      assumption_id: "a-1",
      correction: "B",
      note: null,
      status: "queued",
      created_at: "2026-05-07T12:00:00Z",
      started_at: null,
      completed_at: null,
      transcript_segment_id: null,
    }],
    completion_summary: null,
    call_segments: [],
    ...overrides,
  };
}

function api(initial: GetReviewResponse = review()): ReviewApiClient {
  return {
    getReview: vi.fn().mockResolvedValue(initial),
    confirmAssumption: vi.fn().mockResolvedValue(review({
      uncertain_assumptions: [{ ...initial.uncertain_assumptions[0], status: "corrected", correction: "Madison" }],
    })),
    cancelCallback: vi.fn().mockResolvedValue(review({
      pending_callbacks: [{ ...initial.pending_callbacks[0], status: "cancelled" }],
    })),
    restoreCallback: vi.fn().mockResolvedValue(initial),
    triggerCallback: vi.fn().mockResolvedValue(review({
      pending_callbacks: [{ ...initial.pending_callbacks[0], status: "triggered" }],
    })),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    createSession: vi.fn().mockResolvedValue({ session_id: "fresh-1" }),
  };
}

describe("<ReviewPageClient>", () => {
  beforeEach(() => {
    pushMock.mockClear();
    MockWebSocket.instances.length = 0;
  });

  it("test_review_page_client_hydrates_from_getReview", async () => {
    const client = api();
    render(wrap(<ReviewPageClient locale="zh" sessionId="sess-1" apiClient={client} />));

    expect(screen.getByText(zh.post_call_review.loading)).toBeInTheDocument();
    await screen.findByText("restaurant");
    expect(client.getReview).toHaveBeenCalledWith("sess-1");
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("test_review_page_client_cancel_callback_posts_and_replaces_state", async () => {
    const client = api();
    render(wrap(<ReviewPageClient locale="zh" sessionId="sess-1" apiClient={client} />));

    await userEvent.click(await screen.findByRole("button", { name: zh.post_call_review.cancel }));
    await waitFor(() => expect(client.cancelCallback).toHaveBeenCalledWith("sess-1", "cb-1"));
    expect(await screen.findByText(zh.post_call_review.callback_status_cancelled)).toBeInTheDocument();
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("test_review_page_client_start_new_call_navigates_to_fresh_live_route", async () => {
    const client = api();
    render(wrap(<ReviewPageClient locale="zh" sessionId="sess-1" apiClient={client} />));

    await userEvent.click(await screen.findByRole("button", { name: zh.post_call_review.start_new_call }));
    await userEvent.click(screen.getByRole("button", { name: zh.post_call_review.start_new_call_confirm_primary }));

    await waitFor(() => expect(client.deleteSession).toHaveBeenCalledWith("sess-1"));
    expect(client.createSession).toHaveBeenCalledTimes(1);
    expect(pushMock).toHaveBeenCalledWith("/zh/live/fresh-1");
    expect(MockWebSocket.instances).toHaveLength(0);
  });
});
