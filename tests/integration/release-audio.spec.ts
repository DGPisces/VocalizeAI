import { expect, test, type Locator, type Page } from "@playwright/test";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

type ReleaseAudioCase = {
  scenario_id: string;
  seed: string;
  task: string;
  behavior: string;
  user_lang: "zh" | "en";
  merchant_lang: "zh" | "en";
  merchant_turns: string[];
  checks: Array<{ id: string; description: string; must_pass: boolean }>;
};

type ReleaseEvidence = {
  clientFrames: unknown[];
  serverFrames: unknown[];
  sttTranscript: unknown[];
  ttsEvents: unknown[];
  browserSpeaker: Array<{
    source: string;
    role?: "ai_to_user" | "ai_to_merchant";
    kind?: string;
    bytes?: number;
    scheduled?: boolean;
    queuedSeconds?: number;
  }>;
};

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function loadCase(): ReleaseAudioCase {
  return JSON.parse(
    requiredEnv("VOCALIZE_RELEASE_AUDIO_CASE_JSON"),
  ) as ReleaseAudioCase;
}

async function writeJSON(fileName: string, payload: unknown): Promise<void> {
  const evidenceDir = requiredEnv("VOCALIZE_RELEASE_AUDIO_EVIDENCE_DIR");
  await fs.mkdir(evidenceDir, { recursive: true });
  await fs.writeFile(
    path.join(evidenceDir, fileName),
    `${JSON.stringify(payload, null, 2)}\n`,
    "utf8",
  );
}

async function runPlayback(utterance: string, index: number): Promise<void> {
  const command = requiredEnv("VOCALIZE_RELEASE_AUDIO_PLAY_CMD");
  await execFileAsync("sh", ["-lc", command], {
    env: {
      ...process.env,
      VOCALIZE_RELEASE_AUDIO_UTTERANCE: utterance,
      VOCALIZE_RELEASE_AUDIO_UTTERANCE_INDEX: String(index),
    },
    timeout: 45_000,
  });
}

async function serverFrames(page: Page): Promise<unknown[]> {
  return page.evaluate(() => {
    const state = (
      window as unknown as { __vocalizeReleaseAudio: ReleaseEvidence }
    ).__vocalizeReleaseAudio;
    return state.serverFrames;
  });
}

function hasServerFrame(
  frames: unknown[],
  predicate: (frame: Record<string, unknown>) => boolean,
): boolean {
  return frames.some(
    (frame) =>
      typeof frame === "object" &&
      frame !== null &&
      predicate(frame as Record<string, unknown>),
  );
}

async function isVisible(locator: Locator): Promise<boolean> {
  return locator.isVisible().catch(() => false);
}

async function sendDialNowNudge(
  page: Page,
  releaseCase: ReleaseAudioCase,
): Promise<void> {
  const dialNow = releaseCase.user_lang === "zh" ? "现在打吧" : "dial now";
  const preflightInput = page.getByRole("textbox").first();
  await expect(preflightInput).toBeVisible({ timeout: 10_000 });
  await preflightInput.fill(dialNow);
  await preflightInput.press("Enter");
}

async function drivePreflightToCallListening(
  page: Page,
  releaseCase: ReleaseAudioCase,
): Promise<void> {
  await expect
    .poll(
      async () =>
        hasServerFrame(await serverFrames(page), (frame) => {
          return frame.type === "readiness_change" && frame.passed === true;
        }),
      { timeout: 120_000 },
    )
    .toBeTruthy();

  const handoverTakeoverButton = page
    .locator(".modal.handover-panel")
    .getByRole("button", {
      name: releaseCase.user_lang === "zh" ? "AI 接管" : "AI takeover",
    })
    .first();
  const callPhaseTakeoverButton = page
    .getByRole("button", {
      name:
        releaseCase.user_lang === "zh"
          ? /^(我来接话|我来说)$/
          : /^(Take over|I'm speaking)$/,
    })
    .first();

  await expect
    .poll(
      async () =>
        (await isVisible(handoverTakeoverButton)) ||
        (await isVisible(callPhaseTakeoverButton)),
      { timeout: 20_000 },
    )
    .toBeTruthy();

  if (await isVisible(handoverTakeoverButton)) {
    await expect(handoverTakeoverButton).toBeEnabled({ timeout: 10_000 });
    await handoverTakeoverButton.click();
  } else {
    await expect(callPhaseTakeoverButton).toBeVisible();
    return;
  }

  await expect
    .poll(
      async () =>
        hasServerFrame(await serverFrames(page), (frame) => {
          return frame.type === "mode_ack" && frame.mode === "call_listening";
        }),
      { timeout: 30_000 },
    )
    .toBeTruthy();
}

test("release audio bridge records STT, TTS, BrowserAudioBridge, and judge evidence", async ({
  page,
  baseURL,
}) => {
  const releaseCase = loadCase();
  const backendURL = requiredEnv("VOCALIZE_RELEASE_AUDIO_BACKEND_URL");
  const inputLabel = requiredEnv("VOCALIZE_RELEASE_AUDIO_INPUT_LABEL");
  const evidence: ReleaseEvidence = {
    clientFrames: [],
    serverFrames: [],
    sttTranscript: [],
    ttsEvents: [],
    browserSpeaker: [],
  };

  await page.addInitScript(() => {
    const state = {
      clientFrames: [] as unknown[],
      serverFrames: [] as unknown[],
      browserSpeaker: [] as unknown[],
    };
    Object.defineProperty(window, "__vocalizeReleaseAudio", {
      value: state,
      configurable: false,
    });
    const NativeWebSocket = window.WebSocket;
    window.WebSocket = class InstrumentedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        this.addEventListener("message", async (event) => {
          if (typeof event.data === "string") {
            try {
              state.serverFrames.push(JSON.parse(event.data));
            } catch {
              state.serverFrames.push({ raw: event.data });
            }
          } else {
            const buffer =
              event.data instanceof ArrayBuffer
                ? event.data
                : await (event.data as Blob).arrayBuffer();
            const bytes = new Uint8Array(buffer);
            state.browserSpeaker.push({
              source: "WebSocketAudio",
              kind: "binary_audio",
              role: bytes[0] === 77 ? "ai_to_merchant" : "ai_to_user",
              bytes: Math.max(0, bytes.length - 1),
            });
          }
        });
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
        if (typeof data === "string") {
          try {
            state.clientFrames.push(JSON.parse(data));
          } catch {
            state.clientFrames.push({ raw: data });
          }
        }
        super.send(data);
      }
    };
  });

  // Diagnostic dump on any exit (pass or fail): write whatever the
  // instrumented WebSocket captured to evidence/<scenario>/<seed>/diag.json
  // so a 120s timeout still yields the actual server/client frame stream
  // instead of just the timeout stack.
  async function dumpDiagnostics(stage: string): Promise<void> {
    try {
      const captured = await page.evaluate(() => {
        const state = (
          window as unknown as { __vocalizeReleaseAudio?: ReleaseEvidence }
        ).__vocalizeReleaseAudio;
        return state ?? null;
      });
      await writeJSON("diag.json", {
        stage,
        scenario_id: releaseCase.scenario_id,
        seed: releaseCase.seed,
        captured_present: captured !== null,
        client_frame_count: captured?.clientFrames.length ?? 0,
        server_frame_count: captured?.serverFrames.length ?? 0,
        client_frames: captured?.clientFrames ?? [],
        server_frames: captured?.serverFrames ?? [],
        browser_speaker: captured?.browserSpeaker ?? [],
      });
    } catch (err) {
      try {
        await writeJSON("diag.json", {
          stage,
          error: err instanceof Error ? err.message : String(err),
        });
      } catch {
        /* swallow */
      }
    }
  }

  try {

  const createResponse = await page.request.post(`${backendURL}/api/sessions`, {
    data: { default_lang: releaseCase.user_lang },
  });
  expect(createResponse.ok()).toBeTruthy();
  const session = await createResponse.json();

  // First navigation persists the chosen input device into localStorage so
  // the reloaded session opens its WS with the correct mic already selected.
  // We post the task *after* the reload so the runner that actually emits
  // readiness_change is the same WS the instrumentation is observing —
  // backend READY_TO_DIAL is not in _CONTROL_RECONNECT_PHASES, so frames
  // emitted to the first WS would be lost across reload.
  await page.goto(
    `${baseURL}/${releaseCase.user_lang}/live/${session.session_id}?ws=${encodeURIComponent(session.ws_url)}`,
  );

  const selectedDevice = await page.evaluate(async (label: string) => {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const input = devices.find(
      (device) => device.kind === "audioinput" && device.label.includes(label),
    );
    if (!input) return null;
    localStorage.setItem("vocalize.device.input_id", input.deviceId);
    return { deviceId: input.deviceId, label: input.label };
  }, inputLabel);
  expect(selectedDevice).not.toBeNull();
  await page.reload();

  await expect(page.locator("body")).toBeVisible();

  await page.request.post(
    `${backendURL}/api/sessions/${session.session_id}/task`,
    {
      data: { task: releaseCase.task },
    },
  );

  await sendDialNowNudge(page, releaseCase);
  await drivePreflightToCallListening(page, releaseCase);

  for (const [index, utterance] of releaseCase.merchant_turns.entries()) {
    await runPlayback(utterance, index);
  }

  await expect
    .poll(
      async () => {
        const frames = await page.evaluate(() => {
          const state = (
            window as unknown as { __vocalizeReleaseAudio: ReleaseEvidence }
          ).__vocalizeReleaseAudio;
          return state.serverFrames;
        });
        return frames.filter(
          (frame) =>
            typeof frame === "object" &&
            frame !== null &&
            (frame as { type?: string; role?: string }).type ===
              "transcript_update" &&
            (frame as { role?: string }).role === "merchant_to_ai",
        ).length;
      },
      { timeout: 120_000 },
    )
    .toBeGreaterThan(0);

  await expect
    .poll(
      async () => {
        const frames = await serverFrames(page);
        return frames.filter(
          (frame) =>
            typeof frame === "object" &&
            frame !== null &&
            (frame as { type?: string; role?: string }).type ===
              "transcript_update" &&
            (frame as { role?: string }).role === "ai_to_merchant",
        ).length;
      },
      { timeout: 120_000 },
    )
    .toBeGreaterThan(0);

  await expect
    .poll(
      async () => {
        const capturedSpeaker = await page.evaluate(() => {
          const state = (
            window as unknown as { __vocalizeReleaseAudio: ReleaseEvidence }
          ).__vocalizeReleaseAudio;
          return state.browserSpeaker;
        });
        return capturedSpeaker.some(
          (event) =>
            event.source === "BrowserAudioBridge" &&
            event.role === "ai_to_merchant" &&
            event.scheduled === true,
        );
      },
      { timeout: 120_000 },
    )
    .toBeTruthy();

  const captured = await page.evaluate(() => {
    const state = (
      window as unknown as { __vocalizeReleaseAudio: ReleaseEvidence }
    ).__vocalizeReleaseAudio;
    return state;
  });
  evidence.clientFrames = captured.clientFrames;
  evidence.serverFrames = captured.serverFrames;
  evidence.sttTranscript = captured.serverFrames.filter(
    (frame) =>
      typeof frame === "object" &&
      frame !== null &&
      (frame as { type?: string; role?: string }).type ===
        "transcript_update" &&
      (frame as { role?: string }).role === "merchant_to_ai",
  );
  evidence.ttsEvents = captured.serverFrames.filter(
    (frame) =>
      typeof frame === "object" &&
      frame !== null &&
      (frame as { type?: string; role?: string }).type ===
        "transcript_update" &&
      (frame as { role?: string }).role === "ai_to_merchant",
  );
  evidence.browserSpeaker = captured.browserSpeaker;

  await writeJSON("metadata.json", {
    scenario_id: releaseCase.scenario_id,
    seed: releaseCase.seed,
    language: releaseCase.merchant_lang,
    backend_url: backendURL,
    browser: "chromium",
    input_device: selectedDevice,
    behavior: releaseCase.behavior,
  });
  await writeJSON("frame_log.json", {
    scenario_id: releaseCase.scenario_id,
    seed: releaseCase.seed,
    client_frames: evidence.clientFrames,
    server_frames: evidence.serverFrames,
  });
  await writeJSON("stt_transcript.json", evidence.sttTranscript);
  await writeJSON("tts_events.json", evidence.ttsEvents);
  await writeJSON("browser_speaker.json", {
    source: "BrowserAudioBridge",
    events: evidence.browserSpeaker,
  });
  await writeJSON("raw_capture_summary.json", {
    scenario_id: releaseCase.scenario_id,
    seed: releaseCase.seed,
    captured: {
      sttTranscript: evidence.sttTranscript.length,
      ttsEvents: evidence.ttsEvents.length,
      browserSpeaker: evidence.browserSpeaker.filter(
        (event) =>
          event.source === "BrowserAudioBridge" &&
          event.role === "ai_to_merchant" &&
          event.scheduled === true,
      ).length,
    },
  });

  await dumpDiagnostics("complete");
  } finally {
    await dumpDiagnostics("final");
  }
});
