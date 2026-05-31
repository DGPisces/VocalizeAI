import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";
import { ConnectionStateChip } from "../components/ConnectionStateChip";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

describe("<ConnectionStateChip>", () => {
  it("test_connection_state_chip_renders_loader_when_reconnecting", () => {
    const { rerender } = render(wrap(<ConnectionStateChip state="connected" />));
    expect(screen.queryByRole("status")).toBeNull();

    rerender(wrap(<ConnectionStateChip state="reconnecting" />));
    expect(screen.getByRole("status")).toHaveTextContent(zh.errors.ws_disconnect);
    expect(screen.getByRole("status").querySelector(".spin")).not.toBeNull();
  });
});
