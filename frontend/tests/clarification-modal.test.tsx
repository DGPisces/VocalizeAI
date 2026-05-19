// frontend/tests/clarification-modal.test.tsx — new

import React from "react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { fireEvent, render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ClarificationModal } from "../components/ClarificationModal";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

afterEach(() => { vi.useRealTimers(); });

describe("<ClarificationModal>", () => {
  const request = { field: "party_size", question: "几位？", lang: "zh" as const, timeout_s: 20 };

  it("renders the toast variant by default", () => {
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    expect(screen.getByRole("complementary")).toHaveClass("clarification-toast");
    expect(screen.getByText("几位？")).toBeInTheDocument();
  });

  it("test_existing_countdown_tick_unaffected", () => {
    vi.useFakeTimers();
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    expect(screen.getByText("00:20")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText("00:19")).toBeInTheDocument();
  });

  it("test_countdown_uses_request_timeout_s_not_hardcoded", () => {
    vi.useFakeTimers();
    const { unmount } = render(
      wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />),
    );
    expect(screen.getByText("00:20")).toBeInTheDocument();
    unmount();

    render(wrap(
      <ClarificationModal
        request={{ ...request, timeout_s: 15 }}
        onAck={() => {}}
        onTimeout={() => {}}
      />,
    ));
    expect(screen.getByText("00:15")).toBeInTheDocument();
  });

  it("clicking 回答 expands toast into modal", async () => {
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    fireEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("ESC collapses modal back to toast (does not cancel)", async () => {
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    fireEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("complementary")).toHaveClass("clarification-toast");
  });

  it("test_countdown_pauses_when_expanded_and_user_types", async () => {
    vi.useFakeTimers();
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText("00:19")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a" } });
    act(() => { vi.advanceTimersByTime(5000); });

    expect(screen.getByText("00:19")).toBeInTheDocument();
  });

  it("test_countdown_resumes_after_collapse_without_send", async () => {
    vi.useFakeTimers();
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={() => {}} />));
    fireEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "a" } });
    fireEvent.click(screen.getByRole("dialog"));
    expect(screen.queryByRole("dialog")).toBeNull();

    act(() => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText("00:19")).toBeInTheDocument();
  });

  it("test_modal_does_not_autoclose_on_phase_change_post_call_review", async () => {
    const onAck = vi.fn();
    const onTimeout = vi.fn();
    const { unmount } = render(
      wrap(<ClarificationModal request={request} onAck={onAck} onTimeout={onTimeout} />),
    );
    await userEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    await userEvent.type(screen.getByRole("textbox"), "6");

    unmount();

    expect(onAck).not.toHaveBeenCalled();
    expect(onTimeout).not.toHaveBeenCalled();
  });

  it("submit fires onAck with the typed value", async () => {
    const onAck = vi.fn();
    render(wrap(<ClarificationModal request={request} onAck={onAck} onTimeout={() => {}} />));
    await userEvent.click(screen.getByRole("button", { name: /回答|answer/i }));
    await userEvent.type(screen.getByRole("textbox"), "6");
    await userEvent.click(screen.getByRole("button", { name: /提交|submit/i }));
    expect(onAck).toHaveBeenCalledWith("6");
  });

  it("timeout fires onTimeout silently", () => {
    vi.useFakeTimers();
    const onTimeout = vi.fn();
    render(wrap(<ClarificationModal request={request} onAck={() => {}} onTimeout={onTimeout} />));
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(onTimeout).toHaveBeenCalledTimes(1);
  });
});
