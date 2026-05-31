import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/src/i18n";
import zh from "../messages/zh.json";
import en from "../messages/en.json";
import { TestComponent } from "./fixtures/i18n-test-component";

describe("i18n bootstrap", () => {
  it("renders Chinese strings under locale=zh", () => {
    render(
      <I18nProvider locale="zh" messages={zh}>
        <TestComponent />
      </I18nProvider>,
    );
    expect(screen.getByText("发送")).toBeInTheDocument();
  });

  it("renders English strings under locale=en", () => {
    render(
      <I18nProvider locale="en" messages={en}>
        <TestComponent />
      </I18nProvider>,
    );
    expect(screen.getByText("Send")).toBeInTheDocument();
  });
});
