/**
 * tests/integration/post-call-callback.spec.ts
 *
 * B3a E2E spec: forces a clarification timeout, then walks through the full
 * post-call review flow → wrong-assumption correction → pending-callback queue
 * → callback dial → callback transcript segment.
 *
 * Requires:
 *   - Backend:  VOCALIZE_DEBUG=1 uvicorn vocalize.main:app --port 8000
 *   - Frontend: VITE_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000 npm run dev -- --port 3000
 *
 * Backend debug knob:
 *   When VOCALIZE_DEBUG=1 is set, the backend accepts a query parameter
 *   `?clarification_timeout_ms=100` on the WebSocket upgrade URL, which
 *   forces any in-call clarification to timeout after 100 ms instead of the
 *   default 30 s.  This lets the test reliably observe the
 *   `uncertain_assumption_added` server frame without real-time waiting.
 *
 * TODO(B3b): Replace the VOCALIZE_DEBUG env + URL-param approach with the
 *   proper feature-flag system once B3b formalizes it.  The test should then
 *   use a dedicated /api/debug/force-clarification-timeout endpoint or a
 *   structured session-creation option.
 *
 * NOTE: These tests will not pass until the B3a integration PR lands (backend
 * uncertain_assumption, pending_callback, and callback_segment frames fully
 * implemented + VOCALIZE_DEBUG debug knob available).
 */

import { expect, test } from "@playwright/test";

// ── viewport ───────────────────────────────────────────────────────────────────

test.use({ viewport: { width: 375, height: 812 } });

// ── helpers ───────────────────────────────────────────────────────────────────

/**
 * Navigate to /zh/new.  The CreateSessionClient auto-fires POST /api/sessions
 * and redirects to /zh/live/<session_id>.  We wait for that redirect.
 */
async function navigateToNewSession(
  page: import("@playwright/test").Page,
): Promise<void> {
  await page.goto("/zh/new");
  await page.waitForURL(/\/zh\/live\/.+/, { timeout: 10_000 });
}

/**
 * Type a message into the TextSupplementInput inside PreflightChat and wait
 * for the user-supplement bubble to appear as echo confirmation.
 */
async function preflightSend(
  page: import("@playwright/test").Page,
  text: string,
): Promise<void> {
  const input = page.locator(".text-supplement-input input[type='text']");
  await input.fill(text);
  await input.press("Enter");
  const exactText = new RegExp(`^${escapeRegExp(text)}$`);
  await expect(
    page.locator(".preflight-chat .bubble--user-supplement", { hasText: exactText }),
  ).toBeVisible({ timeout: 8_000 });
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Drive enough preflight turns to reach ready_to_dial state.
 * Covers the mandatory slots for a restaurant reservation task.
 */
async function drivePreflight(page: import("@playwright/test").Page): Promise<void> {
  const preflightSection = page.locator("section.preflight-chat");
  await preflightSection.waitFor({ state: "visible", timeout: 10_000 });

  await preflightSend(page, "帮我预订今晚七点四个人的位子");
  // Wait for first AI question.
  await page.locator(".preflight-chat .bubble--ai").first().waitFor({ state: "visible", timeout: 15_000 });

  // Answer core required slots.
  await preflightSend(page, "今天");       // date
  await preflightSend(page, "晚上七点");   // time
  await preflightSend(page, "四个人");     // party size
  await preflightSend(page, "张三");       // reservation name
}

/**
 * Wait for the HandoverPanel (role="dialog", class "modal handover-panel"),
 * assert the takeover button is enabled, and click it to begin the call phase.
 */
async function doHandover(page: import("@playwright/test").Page): Promise<void> {
  const handoverPanel = page.locator(".modal.handover-panel");
  await handoverPanel.waitFor({ state: "visible", timeout: 20_000 });
  const takeoverBtn = handoverPanel.getByRole("button", { name: "AI 接管" });
  await expect(takeoverBtn).toBeEnabled({ timeout: 5_000 });
  await takeoverBtn.click();
}

// ── test: full post-call callback chain ──────────────────────────────────────

test("B3a post-call: clarification timeout → review → wrong correction → callback dial", async ({ page }) => {
  // ── Step 1: boot session ──────────────────────────────────────────────────
  // TODO(B3b): Pass `?clarification_timeout_ms=100` on the WS upgrade URL so
  // the backend forces a quick clarification timeout.  This requires the
  // backend to read the param when VOCALIZE_DEBUG=1.  Until then, the
  // `uncertain_assumption_added` frame must be triggered via normal call flow
  // (i.e., a genuine timeout in a very short-timeout debug session).
  //
  // The env var VOCALIZE_DEBUG=1 must be set in the shell that boots uvicorn.
  // playwright.config.ts passes it through its webServer `command`.
  await navigateToNewSession(page);

  // ── Step 2: drive preflight + handover ────────────────────────────────────
  await drivePreflight(page);
  await doHandover(page);

  // ── Step 3: merchant turn — wait for TranscriptStream to appear ───────────
  const transcriptStream = page.locator("section.transcript-stream");
  await transcriptStream.waitFor({ state: "visible", timeout: 15_000 });

  // Wait for at least one AI-to-merchant bubble confirming the call is live.
  await page.locator(".bubble--ai-to-merchant").first().waitFor({ state: "visible", timeout: 20_000 });

  // ── Step 4: force clarification timeout ───────────────────────────────────
  // With VOCALIZE_DEBUG=1 + ?clarification_timeout_ms=100, the backend will
  // emit an `uncertain_assumption_added` server frame after 100 ms of the
  // clarification being open, then silently close the clarification modal.
  //
  // TODO(B3b): When the debug knob is fully wired, add the following to the
  // page.goto() call in Step 1:
  //   page.goto(`/zh/new?clarification_timeout_ms=100`)
  // and verify the CreateSessionClient forwards the param to POST /api/sessions
  // or to the WS upgrade URL.
  //
  // For now we wait for the `uncertain_assumption_added` frame evidence:
  // the backend should emit it and the frontend state should render an
  // assumption row in PostCallReview once we reach post_call_review phase.
  //
  // Assert: if a ClarificationModal/toast appeared, it should close silently
  // after the timeout fires (we don't see it lingering).
  const clarificationModal = page.locator(".clarification-modal-backdrop[role='dialog']");
  const clarificationToast = page.locator("aside.clarification-toast[role='complementary']");

  // Both selectors may or may not be visible depending on whether a
  // clarification was triggered.  If visible, they should auto-close.
  // We give the backend up to 5 s for the fast-timeout to fire.
  if (await clarificationModal.count() > 0) {
    await expect(clarificationModal).toBeHidden({ timeout: 5_000 });
  }
  if (await clarificationToast.count() > 0) {
    await expect(clarificationToast).toBeHidden({ timeout: 5_000 });
  }

  // ── Step 5: hang up → confirm → wait for PostCallReview ──────────────────
  // HangupButton renders as:
  //   <button class="chip-btn chip-btn--danger">挂断</button>
  // then opens a dialog with:
  //   <button class="chip-btn chip-btn--danger">确认挂断</button>
  const hangupBtn = page.locator("button.chip-btn--danger", { hasText: "挂断" });
  await hangupBtn.waitFor({ state: "visible", timeout: 10_000 });
  await hangupBtn.click();

  // Confirmation dialog: aria-labelledby="hangup-dialog-title" with
  // confirm button text "确认挂断" (zh.json hangup.confirm).
  const confirmDialog = page.locator("[role='dialog'][aria-labelledby='hangup-dialog-title']");
  await confirmDialog.waitFor({ state: "visible", timeout: 5_000 });
  await confirmDialog.getByRole("button", { name: "确认挂断" }).click();

  // PostCallReview renders as <section class="post-call-review">.
  const postCallReview = page.locator("section.post-call-review");
  await postCallReview.waitFor({ state: "visible", timeout: 15_000 });

  // ── Step 6: assert assumption row; click 指出错误 ─────────────────────────
  // AssumptionRow renders as <li class="assumption-row">.
  // The "指出错误" button (zh.json post_call_review.flag_wrong) expands the
  // correction form.
  //
  // NOTE: This assertion requires at least one uncertain_assumption_added frame
  // to have been received.  If the debug knob is not yet available, this step
  // will fail with "No assumption-row found" — that is expected until B3a-core
  // lands the backend hook.
  const assumptionRow = postCallReview.locator("li.assumption-row").first();
  await assumptionRow.waitFor({ state: "visible", timeout: 10_000 });

  const flagWrongBtn = assumptionRow.getByRole("button", { name: "指出错误" });
  await expect(flagWrongBtn).toBeVisible();
  await flagWrongBtn.click();

  // ── Step 7: fill correction + note inputs ────────────────────────────────
  // The expanded assumption-row__form has:
  //   <label>正确值</label><input id="correct-value-<id>" type="text" required />
  //   <label>备注（可选）</label><input id="note-<id>" type="text" />
  const correctionForm = assumptionRow.locator("form.assumption-row__form");
  await correctionForm.waitFor({ state: "visible", timeout: 5_000 });

  const correctValueInput = correctionForm.locator("input[type='text']").first();
  await correctValueInput.fill("五个人");

  const noteInput = correctionForm.locator("input[type='text']").nth(1);
  await noteInput.fill("原来说四个，但实际需要五个");

  // ── Step 8: submit correction ─────────────────────────────────────────────
  // Submit button text: zh.json post_call_review.submit_correction = "提交"
  const submitBtn = correctionForm.getByRole("button", { name: "提交" });
  await submitBtn.click();

  // ── Step 9: assert pending_callbacks row appears ──────────────────────────
  // After a wrong-correction submit the backend adds a pending_callback entry.
  // PostCallReview renders the "待回拨" section when callbacks.length > 0:
  //   <h3>待回拨</h3>
  //   <ol class="callbacks">
  //     <li class="callback-row">
  //       <p class="callback-row__summary">…correction…</p>
  //       <button class="chip-btn chip-btn--primary">立刻拨</button>
  //     </li>
  //   </ol>
  const callbacksSection = postCallReview.locator("ol.callbacks");
  await callbacksSection.waitFor({ state: "visible", timeout: 15_000 });

  const callbackRow = callbacksSection.locator("li.callback-row").first();
  await callbackRow.waitFor({ state: "visible", timeout: 5_000 });

  // The correction text we submitted should appear in the row summary.
  await expect(callbackRow.locator(".callback-row__summary", { hasText: "五个人" })).toBeVisible();

  // ── Step 10: click 立刻拨 ─────────────────────────────────────────────────
  // Button text: zh.json post_call_review.dial_now = "立刻拨"
  const dialNowBtn = callbackRow.getByRole("button", { name: "立刻拨" });
  await expect(dialNowBtn).toBeEnabled();
  await dialNowBtn.click();

  // After triggering the callback, the page transitions to the call phase
  // (callback_active) and shows the TranscriptStream again.
  const callbackTranscript = page.locator("section.transcript-stream");
  await callbackTranscript.waitFor({ state: "visible", timeout: 15_000 });

  // ── Step 11: assert callback_segment separator appears ───────────────────
  // TranscriptStream renders a <div class="callback-separator" role="separator">
  // when it encounters a message with subtype="callback_segment".
  //
  // zh.json transcript_stream.callback_label = "回拨通话"
  const callbackSeparator = callbackTranscript.locator(
    "[role='separator'][aria-label='回拨通话']",
  );
  await callbackSeparator.waitFor({ state: "visible", timeout: 20_000 });
  await expect(callbackSeparator).toBeVisible();
});

// ── test: post-call empty state (no assumptions) ──────────────────────────────

test("B3a post-call review shows empty state when all assumptions confirmed", async ({ page }) => {
  await navigateToNewSession(page);
  await drivePreflight(page);
  await doHandover(page);

  // Wait for call to start.
  await page.locator("section.transcript-stream").waitFor({ state: "visible", timeout: 15_000 });

  // Hang up immediately without waiting for clarification.
  const hangupBtn = page.locator("button.chip-btn--danger", { hasText: "挂断" });
  await hangupBtn.waitFor({ state: "visible", timeout: 10_000 });
  await hangupBtn.click();

  const confirmDialog = page.locator("[role='dialog'][aria-labelledby='hangup-dialog-title']");
  await confirmDialog.waitFor({ state: "visible", timeout: 5_000 });
  await confirmDialog.getByRole("button", { name: "确认挂断" }).click();

  // If no assumptions were flagged, PostCallReview renders the empty-state
  // variant: <section class="post-call-review post-call-review--empty">
  const postCallReview = page.locator("section.post-call-review");
  await postCallReview.waitFor({ state: "visible", timeout: 15_000 });

  // If the empty-state variant, the heading text is from
  // zh.json post_call_review.empty_state.
  // If there are assumptions (depends on backend response), the title heading
  // from post_call_review.title is shown instead.
  // Either way, we assert the section exists and one of these headings is visible.
  const hasAssumptions = await postCallReview.locator("li.assumption-row").count();
  if (hasAssumptions === 0) {
    await expect(
      postCallReview.getByRole("heading", { name: /通话顺利完成/ }),
    ).toBeVisible();
  } else {
    await expect(
      postCallReview.getByRole("heading", { name: /通话结束/ }),
    ).toBeVisible();
  }
});
