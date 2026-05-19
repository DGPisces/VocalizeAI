import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { CreateSessionClient } from "../app/[locale]/new/CreateSessionClient";
import { NextIntlClientProvider } from "next-intl";
import en from "../messages/en.json";
import zh from "../messages/zh.json";

const replaceMock = vi.hoisted(() => vi.fn());

const wrap = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="zh" messages={zh}>{ui}</NextIntlClientProvider>
);

const wrapEn = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="en" messages={en}>{ui}</NextIntlClientProvider>
);

beforeEach(() => {
  process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "http://127.0.0.1:8000";
  localStorage.clear();
  vi.resetAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

describe("<CreateSessionClient>", () => {
  it("posts auto_translate_merchant=false when localStorage says so", async () => {
    localStorage.setItem("auto_translate_merchant", "false");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: "ws://x/ws/sessions/s-1",
        default_lang: "zh",
        preferred_voice_id: null,
        auto_translate_merchant: false,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const onCreated = vi.fn();
    render(wrap(<CreateSessionClient onCreated={onCreated} />));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string);
    expect(body.auto_translate_merchant).toBe(false);
    expect(body.default_lang).toBe("zh");
  });

  it("defaults to true when localStorage is empty", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: "ws://x/ws/sessions/s-1",
        default_lang: "zh",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(wrap(<CreateSessionClient onCreated={() => {}} />));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string);
    expect(body.auto_translate_merchant).toBe(true);
    expect(body.default_lang).toBe("zh");
  });

  it("uses the route locale as the backend default language", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: "ws://x/ws/sessions/s-1",
        default_lang: "en",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(wrapEn(<CreateSessionClient onCreated={() => {}} />));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string);
    expect(body.default_lang).toBe("en");
  });

  it("redirects to the current UI locale live page with the trusted websocket URL", async () => {
    const wsUrl = "ws://127.0.0.1:8000/ws/sessions/s-1";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: wsUrl,
        default_lang: "zh",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    }));

    render(wrap(<CreateSessionClient onCreated={() => {}} />));

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith(
        `/zh/live/s-1?ws=${encodeURIComponent(wsUrl)}`,
      );
    });
  });

  it("uses stored preferred_ui_lang=en for UI routing without changing default_lang", async () => {
    localStorage.setItem("preferred_ui_lang", "en");
    const wsUrl = "ws://127.0.0.1:8000/ws/sessions/s-1";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: wsUrl,
        default_lang: "zh",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(wrap(<CreateSessionClient onCreated={() => {}} />));

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith(
        `/en/live/s-1?ws=${encodeURIComponent(wsUrl)}`,
      );
    });
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string);
    expect(body.default_lang).toBe("zh");
    expect(body.preferred_ui_lang).toBeUndefined();
  });

  it("uses stored preferred_ui_lang=zh for UI routing without changing default_lang", async () => {
    localStorage.setItem("preferred_ui_lang", "zh");
    const wsUrl = "ws://127.0.0.1:8000/ws/sessions/s-2";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-2",
        ws_url: wsUrl,
        default_lang: "en",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(wrapEn(<CreateSessionClient onCreated={() => {}} />));

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith(
        `/zh/live/s-2?ws=${encodeURIComponent(wsUrl)}`,
      );
    });
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string);
    expect(body.default_lang).toBe("en");
    expect(body.preferred_ui_lang).toBeUndefined();
  });

  it("renders the localized creation status", () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise(() => {})));

    render(wrap(<CreateSessionClient onCreated={() => {}} />));

    expect(document.body).toHaveTextContent("正在创建会话...");
  });

  it("shows an error and retries when session creation fails", async () => {
    const wsUrl = "ws://127.0.0.1:8000/ws/sessions/s-retry";
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("backend offline"))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          session_id: "s-retry",
          ws_url: wsUrl,
          default_lang: "en",
          preferred_voice_id: null,
          auto_translate_merchant: true,
        }),
      });
    vi.stubGlobal("fetch", fetchMock);

    render(wrapEn(<CreateSessionClient onCreated={() => {}} />));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not create session: backend offline",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(replaceMock).toHaveBeenCalledWith(
        `/en/live/s-retry?ws=${encodeURIComponent(wsUrl)}`,
      );
    });
  });
});
