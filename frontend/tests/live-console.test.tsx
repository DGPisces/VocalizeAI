import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocket } from "./setup";
import { postTask } from "../lib/api";
import { LiveConsole } from "../components/LiveConsole";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    postTask: vi.fn().mockResolvedValue(undefined)
  };
});

describe("LiveConsole", () => {
  const validWsUrl = "ws://example.test/ws/sessions/s";

  beforeEach(() => {
    process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "http://example.test";
    delete process.env.NEXT_PUBLIC_VOCALIZE_WS_BASE_URL;
    localStorage.clear();
    vi.mocked(postTask).mockClear();
  });

  it("keeps handover disabled until backend readiness passes", async () => {
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);

    expect(screen.getByRole("button", { name: "AI 接管" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => MockWebSocket.instances[0].emitMessage(JSON.stringify({
      type: "readiness_change",
      passed: true,
      missing_critical: [],
      confidence: 1
    })));

    expect(screen.getByRole("button", { name: "AI 接管" })).not.toBeDisabled();
  });

  it("does not render the test-only readiness override", () => {
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);

    expect(screen.queryByRole("button", { name: "Mark ready (test)" })).not.toBeInTheDocument();
  });

  it("renders backend errors in an alert", () => {
    render(
      <LiveConsole
        sessionId="s"
        wsUrl={validWsUrl}
        initialError="处理失败"
      />
    );

    expect(screen.getByRole("alert")).toHaveTextContent("处理失败");
  });

  it("does not open the WebSocket until task submission succeeds", async () => {
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);

    expect(MockWebSocket.instances).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));

    await waitFor(() => expect(postTask).toHaveBeenCalledWith(
      "s",
      "帮我订今晚七点四个人"
    ));
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toBe(validWsUrl);
  });

  it("rejects cross-origin WebSocket URLs before connecting", async () => {
    render(<LiveConsole sessionId="s" wsUrl="wss://attacker.example/ws/sessions/s" />);

    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));

    await waitFor(() => expect(postTask).toHaveBeenCalled());
    expect(MockWebSocket.instances).toHaveLength(0);
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid WebSocket URL");
  });

  it("routes clarification replies through ack_clarification", async () => {
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);
    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => MockWebSocket.instances[0].emitMessage(JSON.stringify({
      type: "clarification_request",
      field: "allergy",
      question: "请确认是否有人过敏？",
      lang: "zh",
      timeout_s: 25
    })));

    expect(await screen.findByText("请确认是否有人过敏？")).toBeVisible();
    fireEvent.change(screen.getByLabelText("回答商家补充问题"), {
      target: { value: "没有过敏" }
    });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(MockWebSocket.instances[0].sent).toContain(JSON.stringify({
      type: "ack_clarification",
      slot_value: "没有过敏"
    }));
    expect(MockWebSocket.instances[0].sent).not.toContain(JSON.stringify({
      type: "text_input",
      text: "没有过敏",
      lang_hint: "zh"
    }));
  });

  it("samples audio diagnostics instead of appending every PCM packet", async () => {
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);
    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    act(() => {
      MockWebSocket.instances[0].emitMessage(new Uint8Array([85, 1, 2]).buffer);
      MockWebSocket.instances[0].emitMessage(new Uint8Array([85, 3, 4]).buffer);
      MockWebSocket.instances[0].emitMessage(new Uint8Array([85, 5, 6]).buffer);
    });

    expect(screen.getAllByText(/audio:ai_to_user:/)).toHaveLength(1);
  });

  it("persists device selection and sends set_devices", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: vi.fn().mockResolvedValue([
          { kind: "audioinput", deviceId: "mic-1", label: "Desk mic" },
          { kind: "audiooutput", deviceId: "spk-1", label: "Desk speaker" }
        ])
      }
    });
    render(<LiveConsole sessionId="s" wsUrl={validWsUrl} />);
    fireEvent.change(screen.getByLabelText("你要 AI 帮你打什么电话？"), {
      target: { value: "帮我订今晚七点四个人" }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));

    fireEvent.change(await screen.findByLabelText("Microphone"), {
      target: { value: "mic-1" }
    });
    fireEvent.change(await screen.findByLabelText("Speaker"), {
      target: { value: "spk-1" }
    });

    expect(localStorage.getItem("vocalize.inputDeviceId")).toBe("mic-1");
    expect(localStorage.getItem("vocalize.outputDeviceId")).toBe("spk-1");
    expect(MockWebSocket.instances[0].sent).toContain(JSON.stringify({
      type: "set_devices",
      input_id: "mic-1",
      output_id: "spk-1",
      aec: true
    }));
  });
});
