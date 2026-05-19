import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  createSession,
  getSession,
  type CreateSessionRequest,
  type GetSessionResponse,
} from "../lib/api";

beforeEach(() => {
  process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL = "http://127.0.0.1:8000";
  vi.resetAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createSession", () => {
  it("posts the new body fields and returns extended response", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: "ws://127.0.0.1:8000/ws/sessions/s-1",
        default_lang: "zh",
        preferred_voice_id: "voice-42",
        auto_translate_merchant: false,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const req: CreateSessionRequest = {
      preferred_voice_id: "voice-42",
      auto_translate_merchant: false,
    };
    const res = await createSession(req);
    expect(res.preferred_voice_id).toBe("voice-42");
    expect(res.auto_translate_merchant).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/sessions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(req),
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
  });

  it("posts an empty body when no options given", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        ws_url: "x",
        default_lang: "zh",
        preferred_voice_id: null,
        auto_translate_merchant: true,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    await createSession();
    const call = fetchMock.mock.calls[0]?.[1];
    expect(call?.body).toBe("{}");
  });
});

describe("getSession", () => {
  it("returns phase + uncertain_assumptions + pending_callbacks", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        session_id: "s-1",
        default_lang: "zh",
        task_description: "demo",
        preferred_voice_id: null,
        auto_translate_merchant: true,
        phase: "post_call_review",
        uncertain_assumptions: [
          {
            id: "a-1",
            slot: "party_size",
            assumed_value: 4,
            source: "user_timeout",
            created_at: "x",
            status: "pending_review",
            question: "?",
            correction: null,
            note: null,
            callback_id: null,
          },
        ],
        pending_callbacks: [],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const res: GetSessionResponse = await getSession("s-1");
    expect(res.phase).toBe("post_call_review");
    expect(res.uncertain_assumptions).toHaveLength(1);
    expect(res.uncertain_assumptions[0].slot).toBe("party_size");
    expect(res.pending_callbacks).toEqual([]);
  });
});
