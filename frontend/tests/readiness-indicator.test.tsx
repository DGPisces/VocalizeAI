// frontend/tests/readiness-indicator.test.tsx

import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ReadinessIndicator } from "../components/ReadinessIndicator";
import { NextIntlClientProvider } from "next-intl";
import zh from "../messages/zh.json";

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

describe("<ReadinessIndicator>", () => {
  it("passed=true renders .alert--ok with ready text", () => {
    const { container } = render(
      wrap(<ReadinessIndicator passed={true} missing_critical={[]} confidence={1} />)
    );
    expect(container.querySelector(".alert--ok")).toBeInTheDocument();
    expect(screen.getByText("信息已足够，可以接管")).toBeInTheDocument();
  });

  it("passed=false renders .alert--warn with waiting text", () => {
    const { container } = render(
      wrap(<ReadinessIndicator passed={false} missing_critical={[]} confidence={0} />)
    );
    expect(container.querySelector(".alert--warn")).toBeInTheDocument();
    expect(screen.getByText("等待关键信息")).toBeInTheDocument();
  });

  it("missing_critical non-empty renders .alert__detail", () => {
    const { container } = render(
      wrap(
        <ReadinessIndicator
          passed={false}
          missing_critical={["name", "time"]}
          confidence={0}
        />
      )
    );
    expect(container.querySelector(".alert__detail")).toBeInTheDocument();
    expect(screen.getByText(/name, time/)).toBeInTheDocument();
  });

  it("missing_critical empty does NOT render .alert__detail", () => {
    const { container } = render(
      wrap(<ReadinessIndicator passed={false} missing_critical={[]} confidence={0} />)
    );
    expect(container.querySelector(".alert__detail")).toBeNull();
  });
});
