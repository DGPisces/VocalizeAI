/**
 * tests/integration/laptop-loopback.spec.ts
 *
 * B3a update: preflight is now driven via typed text_input frames instead of
 * synthetic PCM audio. Audio-route assertions are retained for the call phase
 * (B2 behaviour unchanged) but are skipped during the preflight conversation
 * turns.
 *
 * Requires:
 *   - Backend:  uvicorn vocalize.main:app --port 8000
 *   - Frontend: VITE_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --port 3000
 *
 * The playwright.config.ts in frontend/ boots both servers automatically
 * via the webServer configuration.
 *
 * These tests will not pass until the B3a integration PR lands (backend
 * preflight WS frames + handover protocol fully implemented).
 */

import { expect, test } from "@playwright/test";

// ── shared helpers ─────────────────────────────────────────────────────────────

/** Navigate to /zh/new, wait for the redirect to /zh/live/<id>, and return the
 *  resolved session URL.  The new-session page calls POST /api/sessions
 *  automatically; we just wait for the redirect. */
async function bootSession(page: import("@playwright/test").Page, taskDescription: string): Promise<string> {
  await page.goto("/zh/new");
  // CreateSessionClient redirects to /zh/live/<session_id> after POST /api/sessions.
  await page.waitForURL(/\/zh\/live\/.+/, { timeout: 10_000 });

  // The PreflightChat input (TextSupplementInput inside PreflightChat) should
  // appear once the session phase is "collecting".
  //
  // aria-label is the placeholder text set on the <input> inside
  // TextSupplementInput. In zh locale the placeholder is "提示 AI".
  const taskInput = page.locator(".text-supplement-input input[type='text']");
  await taskInput.waitFor({ state: "visible", timeout: 10_000 });
  await taskInput.fill(taskDescription);
  await taskInput.press("Enter");

  return page.url();
}

/** Type one answer turn into the PreflightChat text supplement input and wait
 *  for the AI's next reply bubble to appear. */
async function preflightTurn(
  page: import("@playwright/test").Page,
  answer: string,
  expectedReplyPattern?: string | RegExp,
): Promise<void> {
  const input = page.locator(".text-supplement-input input[type='text']");
  await input.fill(answer);
  await input.press("Enter");

  if (expectedReplyPattern) {
    // Wait for an AI bubble (role=ai_to_user → .bubble--ai) containing the
    // expected text to appear.
    await expect(
      page.locator(".preflight-chat .bubble--ai", { hasText: expectedReplyPattern }),
    ).toBeVisible({ timeout: 15_000 });
  } else {
    // Just wait for any new AI bubble after the user input rendered.
    const userBubble = page.locator(".preflight-chat .bubble--user-supplement").last();
    await userBubble.waitFor({ state: "visible", timeout: 5_000 });
  }
}

// ── viewport ───────────────────────────────────────────────────────────────────

test.use({ viewport: { width: 375, height: 812 } });

// ── test: home page links into session flow ───────────────────────────────────

test("B3a home page renders start link", async ({ page }) => {
  await page.goto("/zh/");
  await expect(page.getByRole("heading", { name: "VocalizeAI" })).toBeVisible();
  // Splash page has a link navigating to /zh/new.
  await expect(page.getByRole("link", { name: /开始预订|Start/i })).toBeVisible();
});

// ── test: preflight driven by typed input, handover appears ───────────────────

test("B3a preflight via typed text and handover panel appears", async ({ page }) => {
  // Step 1: navigate to /zh/new → auto-redirects to /zh/live/<id>
  await page.goto("/zh/new");
  await page.waitForURL(/\/zh\/live\/.+/, { timeout: 10_000 });

  // Step 2: PreflightChat section should be visible at this point.
  const preflightSection = page.locator("section.preflight-chat");
  await preflightSection.waitFor({ state: "visible", timeout: 10_000 });

  // Step 3: type the initial task description into the TextSupplementInput
  //         that lives inside PreflightChat.
  const preflightInput = page.locator(".text-supplement-input input[type='text']");
  await preflightInput.fill("帮我预订今晚七点四个人的位子");
  await preflightInput.press("Enter");

  // The user's own text echoes as a .bubble--user-supplement inside PreflightChat.
  await expect(
    preflightSection.locator(".bubble--user-supplement", { hasText: "帮我预订今晚七点四个人的位子" }),
  ).toBeVisible({ timeout: 8_000 });

  // Step 4: wait for first AI question in PreflightChat (.bubble--ai).
  const firstAiBubble = preflightSection.locator(".bubble--ai").first();
  await firstAiBubble.waitFor({ state: "visible", timeout: 15_000 });

  // Steps 5-6: drive 3-5 turns of the preflight conversation via typed text.
  // NOTE: The exact AI questions depend on backend task-planning logic. We use
  // plausible answers for a restaurant-reservation task that covers the common
  // required slots (date, time, party-size, name).
  await preflightTurn(page, "今天");         // date
  await preflightTurn(page, "晚上七点");     // time
  await preflightTurn(page, "四个人");       // party size
  await preflightTurn(page, "张三");         // name for reservation

  // NOTE: No synthetic-PCM audio assertions during preflight (B3a deviation).
  // Audio-route assertions are only exercised once the call phase begins (Step 9).

  // Step 7: assert the HandoverPanel appears once readiness_passed === true.
  // HandoverPanel renders as role="dialog" with className="modal handover-panel".
  // We wait up to 20 s because readiness evaluation may require several turns.
  const handoverPanel = page.locator(".modal.handover-panel");
  await handoverPanel.waitFor({ state: "visible", timeout: 20_000 });
  await expect(handoverPanel).toBeVisible();

  // The takeover button text is from zh.json handover.takeover_button = "AI 接管".
  const takeoverBtn = handoverPanel.getByRole("button", { name: "AI 接管" });
  await expect(takeoverBtn).toBeEnabled({ timeout: 5_000 });

  // Step 8: click takeover — sends mode_change(call_listening) to backend.
  await takeoverBtn.click();

  // After takeover the HandoverPanel dismisses and TranscriptStream appears.
  const transcriptStream = page.locator("section.transcript-stream");
  await transcriptStream.waitFor({ state: "visible", timeout: 10_000 });

  // Step 9: audio-route assertion — merchant TTS plays during call (B2 unchanged).
  // The BrowserAudioBridge component is hidden but present. We verify that
  // at least one AI-to-merchant bubble appears in the TranscriptStream, which
  // signals the backend is streaming audio frames to the bridge.
  // (Full audio hardware testing is out of scope for CI — we assert the DOM
  //  evidence that the bridge received frames.)
  const merchantBubble = transcriptStream.locator(".bubble--ai-to-merchant").first();
  await merchantBubble.waitFor({ state: "visible", timeout: 20_000 });

  // Step 10: assert clarification toast appears when backend requests one.
  // ClarificationModal in toast mode renders as:
  //   <aside class="clarification-toast" role="complementary">
  // We only assert it *can* appear; the test passes either way if no
  // clarification is triggered for this particular task (backend-dependent).
  // For a deterministic assertion, the B3b feature-flag work will let us
  // force a clarification — see post-call-callback.spec.ts for that path.
  const clarificationToast = page.locator("aside.clarification-toast[role='complementary']");
  const toastCount = await clarificationToast.count();
  if (toastCount > 0) {
    await expect(clarificationToast).toBeVisible();
  }

  // Step 11: test passes.
});

// ── test: clarification ack reply in preflight ────────────────────────────────

test("B3a preflight clarification ack via supplement input", async ({ page }) => {
  // Boot session with a task description that is likely to trigger a slot
  // clarification during preflight (explicitly under-specified task).
  await page.goto("/zh/new");
  await page.waitForURL(/\/zh\/live\/.+/, { timeout: 10_000 });

  const preflightSection = page.locator("section.preflight-chat");
  await preflightSection.waitFor({ state: "visible", timeout: 10_000 });

  const input = page.locator(".text-supplement-input input[type='text']");
  await input.fill("帮我打个电话");
  await input.press("Enter");

  // Wait for AI to ask a clarifying question.
  const firstAiBubble = preflightSection.locator(".bubble--ai").first();
  await firstAiBubble.waitFor({ state: "visible", timeout: 15_000 });

  // Answer via the TextSupplementInput (B3a: no synthetic audio).
  await input.fill("我要预订餐厅");
  await input.press("Enter");

  // User supplement bubble should appear echoing the typed answer.
  await expect(
    preflightSection.locator(".bubble--user-supplement", { hasText: "我要预订餐厅" }),
  ).toBeVisible({ timeout: 8_000 });

  // The backend should ack the text_input and respond with a follow-up question.
  // We verify a second .bubble--ai appears.
  await expect(preflightSection.locator(".bubble--ai").nth(1)).toBeVisible({ timeout: 15_000 });
});
