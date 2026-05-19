import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";
import en from "../messages/en.json";
import { TestComponent } from "./fixtures/i18n-test-component";

describe("i18n bootstrap", () => {
  it("renders Chinese strings under locale=zh", () => {
    render(
      <NextIntlClientProvider locale="zh" messages={zh}>
        <TestComponent />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("发送")).toBeInTheDocument();
  });

  it("renders English strings under locale=en", () => {
    render(
      <NextIntlClientProvider locale="en" messages={en}>
        <TestComponent />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText("Send")).toBeInTheDocument();
  });
});
