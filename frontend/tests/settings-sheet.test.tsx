// frontend/tests/settings-sheet.test.tsx

import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider } from "@/src/i18n";
import { Settings } from "../components/Settings";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

const defaultProps = {
  open: true,
  onClose: vi.fn(),
  autoTranslate: false,
  onAutoTranslateChange: vi.fn(),
};

describe("<Settings>", () => {
  it("open=false renders nothing", () => {
    const { container } = render(
      wrap(<Settings {...defaultProps} open={false} />)
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("open=true renders dialog with DeviceSettings child", () => {
    render(wrap(<Settings {...defaultProps} />));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // DeviceSettings section should be present
    expect(screen.getByRole("region")).toBeInTheDocument();
  });

  it("ESC fires onClose", async () => {
    const onClose = vi.fn();
    render(wrap(<Settings {...defaultProps} onClose={onClose} />));
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("click outside backdrop fires onClose", async () => {
    const onClose = vi.fn();
    render(wrap(<Settings {...defaultProps} onClose={onClose} />));
    // Click the backdrop div (the dialog element itself, not the aside)
    const backdrop = screen.getByRole("dialog");
    await userEvent.pointer({ target: backdrop, keys: "[MouseLeft]" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
