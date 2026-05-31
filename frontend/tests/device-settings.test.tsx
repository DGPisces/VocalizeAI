// frontend/tests/device-settings.test.tsx

import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider } from "@/src/i18n";
import { DeviceSettings } from "../components/DeviceSettings";
import zh from "../messages/zh.json";
import en from "../messages/en.json";
import type { DevicePreferences } from "../components/DeviceSettings";

const wrap = (ui: React.ReactNode) => (
  <I18nProvider locale="zh" messages={zh}>{ui}</I18nProvider>
);

const wrapEn = (ui: React.ReactNode) => (
  <I18nProvider locale="en" messages={en}>{ui}</I18nProvider>
);

const mockDevices: MediaDeviceInfo[] = [
  {
    deviceId: "mic-1",
    kind: "audioinput",
    label: "Built-in Microphone",
    groupId: "grp-1",
    toJSON: () => ({}),
  },
  {
    deviceId: "mic-2",
    kind: "audioinput",
    label: "USB Microphone",
    groupId: "grp-2",
    toJSON: () => ({}),
  },
  {
    deviceId: "spk-1",
    kind: "audiooutput",
    label: "Built-in Speaker",
    groupId: "grp-3",
    toJSON: () => ({}),
  },
];

function createMockStream(): MediaStream {
  return {
    getTracks: () => [{ stop: vi.fn() }],
  } as unknown as MediaStream;
}

function installMediaRecorderMock() {
  const instances: Array<{
    stream: MediaStream;
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    ondataavailable: ((event: BlobEvent) => void) | null;
    onstop: (() => void) | null;
  }> = [];

  class MockMediaRecorder {
    stream: MediaStream;
    start = vi.fn();
    stop = vi.fn(() => {
      this.ondataavailable?.({ data: new Blob(["mic-test"], { type: "audio/webm" }) } as BlobEvent);
      this.onstop?.();
    });
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;

    constructor(stream: MediaStream) {
      this.stream = stream;
      instances.push(this);
    }
  }

  vi.stubGlobal("MediaRecorder", MockMediaRecorder);
  return instances;
}

beforeEach(() => {
  vi.useRealTimers();
  localStorage.clear();
  Object.defineProperty(globalThis.navigator, "mediaDevices", {
    configurable: true,
    value: {
      enumerateDevices: vi.fn().mockResolvedValue(mockDevices),
      getUserMedia: vi.fn().mockResolvedValue(createMockStream()),
    },
  });
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:mic-test"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  Reflect.deleteProperty(window.HTMLMediaElement.prototype, "setSinkId");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("<DeviceSettings>", () => {
  it("populates mic + speaker dropdowns from enumerateDevices", async () => {
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => {
      expect(screen.getByText("Built-in Microphone")).toBeInTheDocument();
      expect(screen.getByText("USB Microphone")).toBeInTheDocument();
      expect(screen.getByText("Built-in Speaker")).toBeInTheDocument();
    });
  });

  it("selecting an input persists to localStorage and emits DevicePreferences", async () => {
    localStorage.setItem("vocalize.device.output_id", "spk-1");
    localStorage.setItem("vocalize.device.aec", "false");
    const onDevicePreferencesChange = vi.fn<(preferences: DevicePreferences) => void>();
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          onDevicePreferencesChange={onDevicePreferencesChange}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());

    // Find the microphone select (first select in the component)
    const selects = screen.getAllByRole("combobox");
    await userEvent.selectOptions(selects[0], "mic-1");

    expect(localStorage.getItem("vocalize.device.input_id")).toBe("mic-1");
    expect(onDevicePreferencesChange).toHaveBeenCalledWith({
      inputId: "mic-1",
      outputId: "spk-1",
      aec: false,
    });
  });

  it("selecting a speaker persists to localStorage and emits DevicePreferences", async () => {
    localStorage.setItem("vocalize.device.input_id", "mic-2");
    const onDevicePreferencesChange = vi.fn<(preferences: DevicePreferences) => void>();
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          onDevicePreferencesChange={onDevicePreferencesChange}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Speaker")).toBeInTheDocument());

    const selects = screen.getAllByRole("combobox");
    await userEvent.selectOptions(selects[1], "spk-1");

    expect(localStorage.getItem("vocalize.device.output_id")).toBe("spk-1");
    expect(onDevicePreferencesChange).toHaveBeenCalledWith({
      inputId: "mic-2",
      outputId: "spk-1",
      aec: true,
    });
  });

  it("AEC toggle persists to localStorage and emits DevicePreferences", async () => {
    localStorage.setItem("vocalize.device.input_id", "mic-1");
    localStorage.setItem("vocalize.device.output_id", "spk-1");
    const onDevicePreferencesChange = vi.fn<(preferences: DevicePreferences) => void>();
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          onDevicePreferencesChange={onDevicePreferencesChange}
        />
      )
    );
    // AEC is on by default; toggle it off
    const checkboxes = screen.getAllByRole("checkbox");
    // aec checkbox is first (index 0), auto-translate is second (index 1)
    await userEvent.click(checkboxes[0]);
    expect(localStorage.getItem("vocalize.device.aec")).toBe("false");
    expect(onDevicePreferencesChange).toHaveBeenCalledWith({
      inputId: "mic-1",
      outputId: "spk-1",
      aec: false,
    });
  });

  it("auto-translate toggle fires onAutoTranslateChange", async () => {
    const onAutoTranslateChange = vi.fn();
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={onAutoTranslateChange}
        />
      )
    );
    const checkboxes = screen.getAllByRole("checkbox");
    // auto-translate is the second checkbox (index 1)
    await userEvent.click(checkboxes[1]);
    expect(onAutoTranslateChange).toHaveBeenCalledWith(true);
  });

  it("disables microphone and AEC controls while devices are switching", async () => {
    render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "switching" }}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());

    const selects = screen.getAllByRole("combobox");
    const checkboxes = screen.getAllByRole("checkbox");

    expect(selects[0]).toBeDisabled();
    expect(selects[1]).not.toBeDisabled();
    expect(checkboxes[0]).toBeDisabled();
    expect(checkboxes[1]).not.toBeDisabled();
  });

  it("renders localized active-call switching statuses with approved variants", async () => {
    const { rerender } = render(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "switching" }}
        />
      )
    );

    let status = await screen.findByRole("status");
    expect(status).toHaveTextContent("正在切换设备…");
    expect(status).toHaveClass("device-settings__status", "device-settings__status--switching");

    rerender(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "success" }}
        />
      )
    );
    status = await screen.findByRole("status");
    expect(status).toHaveTextContent("设备已切换");
    expect(status).toHaveClass("device-settings__status--success");

    rerender(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "switch_error" }}
        />
      )
    );
    status = await screen.findByRole("status");
    expect(status).toHaveTextContent("无法切换麦克风，已恢复到上一个可用设备。请检查浏览器权限后再试。");
    expect(status).toHaveClass("device-settings__status--error");

    rerender(
      wrap(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "speaker_fallback" }}
        />
      )
    );
    status = await screen.findByRole("status");
    expect(status).toHaveTextContent("当前浏览器不支持指定扬声器输出，已使用默认扬声器。");
    expect(status).toHaveClass("device-settings__status--warning");
  });

  it("renders English active-call switching status copy", async () => {
    const { rerender } = render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "switching" }}
        />
      )
    );

    expect(await screen.findByRole("status")).toHaveTextContent("Switching devices…");

    rerender(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "success" }}
        />
      )
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Device switched");

    rerender(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "switch_error" }}
        />
      )
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Could not switch microphone. Restored the previous working device. Check browser permissions and try again.",
    );

    rerender(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          deviceSwitchStatus={{ kind: "speaker_fallback" }}
        />
      )
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "This browser does not support selecting speaker output; using the default speaker.",
    );
  });

  it("records a fixed 3-second local microphone sample and exposes playback", async () => {
    const mediaRecorderInstances = installMediaRecorderMock();
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia);
    const createObjectURL = vi.mocked(URL.createObjectURL);

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          devicePreferences={{ inputId: "mic-1", outputId: "spk-1", aec: false }}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());
    vi.useFakeTimers();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test microphone" }));
      await Promise.resolve();
    });

    expect(getUserMedia).toHaveBeenCalledWith({
      audio: expect.objectContaining({
        deviceId: { exact: "mic-1" },
        echoCancellation: false,
      }),
    });
    expect(mediaRecorderInstances[0].start).toHaveBeenCalled();
    expect(screen.getByText("Recording; stops automatically in 3 seconds…")).toBeInTheDocument();
    expect(screen.getByLabelText("Microphone level")).toBeInTheDocument();
    expect(screen.getAllByRole("combobox")[0]).toBeDisabled();
    expect(screen.getAllByRole("checkbox")[0]).toBeDisabled();
    expect(screen.getByRole("button", { name: "Test microphone" })).toBeDisabled();

    await act(async () => {
      vi.advanceTimersByTime(2999);
    });
    expect(createObjectURL).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(1);
    });

    expect(mediaRecorderInstances[0].stop).toHaveBeenCalled();
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Play recording" })).toBeInTheDocument();
    expect(document.querySelector("audio")?.getAttribute("src")).toBe("blob:mic-test");
  });

  it("revokes replaced and unmounted microphone recording URLs", async () => {
    installMediaRecorderMock();
    const createObjectURL = vi.mocked(URL.createObjectURL);
    const revokeObjectURL = vi.mocked(URL.revokeObjectURL);
    createObjectURL.mockReturnValueOnce("blob:first").mockReturnValueOnce("blob:second");

    const { unmount } = render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());
    vi.useFakeTimers();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test microphone" }));
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByRole("button", { name: "Play recording" })).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test microphone" }));
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(revokeObjectURL).toHaveBeenCalledWith("blob:first");
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:second");
  });

  it("keeps live microphone level available when recording playback is unsupported", async () => {
    vi.stubGlobal("MediaRecorder", undefined);

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());
    vi.useFakeTimers();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test microphone" }));
      await Promise.resolve();
    });

    expect(screen.getByText("This browser does not support recording playback, but live level still works.")).toBeInTheDocument();
    expect(screen.getByLabelText("Microphone level")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.queryByRole("button", { name: "Play recording" })).not.toBeInTheDocument();
  });

  it("shows a permission hint when microphone test permission fails", async () => {
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(
      new DOMException("Denied", "NotAllowedError"),
    );

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test microphone" }));
      await Promise.resolve();
    });

    expect(screen.getByText("Allow microphone access to show real device names.")).toBeInTheDocument();
    expect(screen.queryByText("Recording; stops automatically in 3 seconds…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test microphone" })).not.toBeDisabled();
  });

  it("plays a local speaker tone through the selected output without backend calls", async () => {
    const play = vi.spyOn(window.HTMLMediaElement.prototype, "play").mockResolvedValue();
    const setSinkId = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.HTMLMediaElement.prototype, "setSinkId", {
      configurable: true,
      value: setSinkId,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          devicePreferences={{ inputId: "mic-1", outputId: "spk-1", aec: true }}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Speaker")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test speaker" }));
      await Promise.resolve();
    });

    expect(setSinkId).toHaveBeenCalledWith("spk-1");
    expect(play).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the speaker fallback warning when output routing is unsupported", async () => {
    vi.spyOn(window.HTMLMediaElement.prototype, "play").mockResolvedValue();

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
          devicePreferences={{ inputId: "", outputId: "spk-1", aec: true }}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Speaker")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Test speaker" }));
      await Promise.resolve();
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      "This browser does not support selecting speaker output; using the default speaker.",
    );
  });

  it("refreshes devices by requesting microphone permission and stopping tracks immediately", async () => {
    const stop = vi.fn();
    const getUserMedia = vi.mocked(navigator.mediaDevices.getUserMedia);
    getUserMedia.mockResolvedValueOnce({
      getTracks: () => [{ stop }],
    } as unknown as MediaStream);

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refresh devices" }));
      await Promise.resolve();
    });

    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(stop).toHaveBeenCalled();
    expect(navigator.mediaDevices.enumerateDevices).toHaveBeenCalledTimes(2);
  });

  it("shows a permission hint after refresh permission denial and keeps devices selectable", async () => {
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValueOnce(new DOMException("Denied", "NotAllowedError"));

    render(
      wrapEn(
        <DeviceSettings
          autoTranslate={false}
          onAutoTranslateChange={vi.fn()}
        />
      )
    );
    await waitFor(() => expect(screen.getByText("Built-in Microphone")).toBeInTheDocument());

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refresh devices" }));
      await Promise.resolve();
    });

    expect(screen.getByText("Allow microphone access to show real device names.")).toBeInTheDocument();
    expect(screen.getAllByRole("combobox")[0]).not.toBeDisabled();
    expect(screen.getAllByRole("combobox")[1]).not.toBeDisabled();
  });
});
