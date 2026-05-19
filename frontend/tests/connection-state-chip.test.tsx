import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import { ConnectionStateChip } from "../components/ConnectionStateChip";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
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
