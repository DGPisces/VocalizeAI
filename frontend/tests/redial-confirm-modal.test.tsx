import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import { RedialConfirmModal } from "../components/RedialConfirmModal";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

describe("<RedialConfirmModal>", () => {
  it("test_start_new_call_confirm_modal_renders_correct_bilingual_copy", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(wrap(<RedialConfirmModal onCancel={onCancel} onConfirm={onConfirm} />));

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(zh.post_call_review.start_new_call_confirm_heading)).toBeInTheDocument();
    expect(screen.getByText(zh.post_call_review.start_new_call_confirm_body)).toBeInTheDocument();
    expect(screen.queryByText(/Redial/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: zh.post_call_review.start_new_call_confirm_primary })).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: zh.post_call_review.start_new_call_confirm_primary }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
