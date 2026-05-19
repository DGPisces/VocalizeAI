import React from "react";
import { act, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import zh from "../messages/zh.json";
import { SessionRecoveredToast } from "../components/SessionRecoveredToast";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

afterEach(() => {
  vi.useRealTimers();
});

describe("<SessionRecoveredToast>", () => {
  it("test_session_recovered_toast_auto_dismisses_after_5s", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();

    render(wrap(<SessionRecoveredToast onDismiss={onDismiss} />));

    expect(screen.getByRole("status")).toHaveTextContent(zh.session.recovered);
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
