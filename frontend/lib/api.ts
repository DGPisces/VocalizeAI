import type {
  CallSegment,
  CallbackEntry,
  SlotAssumption,
  TranscriptMessage,
} from "./state";
import type { TaskPhaseValue } from "./ws";

export interface CreateSessionRequest {
  preferred_voice_id?: string | null; // null = clear/unset
  auto_translate_merchant?: boolean;
  default_lang?: "zh" | "en";
}

export interface SessionResponse {
  session_id: string;
  ws_url: string;
  default_lang: "zh" | "en";
  preferred_voice_id: string | null;
  auto_translate_merchant: boolean;
}

export interface GetSessionResponse {
  session_id: string;
  default_lang: "zh" | "en";
  task_description: string | null;
  preferred_voice_id: string | null;
  auto_translate_merchant: boolean;
  phase: TaskPhaseValue;
  uncertain_assumptions: SlotAssumption[];
  pending_callbacks: CallbackEntry[];
}

export interface ReviewCallSegment extends CallSegment {
  transcript: TranscriptMessage[];
}

export interface GetReviewResponse {
  session_id: string;
  status: "completed" | "interrupted" | "escalated";
  slots: Record<string, unknown>;
  uncertain_assumptions: SlotAssumption[];
  pending_callbacks: CallbackEntry[];
  completion_summary: string | null;
  call_segments: ReviewCallSegment[];
}

function apiBaseUrl(): string {
  const value = process.env.NEXT_PUBLIC_VOCALIZE_API_BASE_URL;
  if (!value) {
    throw new Error("NEXT_PUBLIC_VOCALIZE_API_BASE_URL is required");
  }
  return value.replace(/\/$/, "");
}

/**
 * Returns the invite token from the build-time env var, or null when unset.
 *
 * Returning null (not throwing) is intentional: localhost-dev must work without
 * any token configured.
 *
 * WARNING: NEXT_PUBLIC_ vars are embedded in the client-side JS bundle and are
 * therefore recoverable by any user who can load the page. This is an accepted
 * v1 limitation — the shared token provides friction, not true access control.
 * Per-user auth (AUTH-01, v1.x) will replace this gate entirely.
 *
 * For stronger protection before AUTH-01 lands, consider routing the session
 * creation call through a Next.js API route that reads the token from a
 * server-side env var (no NEXT_PUBLIC_ prefix) so it never reaches the bundle.
 */
function inviteToken(): string | null {
  const value = process.env.NEXT_PUBLIC_VOCALIZE_INVITE_TOKEN;
  return value && value.length > 0 ? value : null;
}

export async function createSession(
  body: CreateSessionRequest = {},
): Promise<SessionResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = inviteToken();
  if (token !== null) {
    headers["X-Invite-Token"] = token;
  }
  const res = await fetch(`${apiBaseUrl()}/api/sessions`, {
    method: "POST",
    cache: "no-store",
    headers,
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // 401 = invite-required per D-08; bubble to caller for user-facing "invite required" message
    throw new Error(`createSession failed: ${res.status}`);
  }
  return (await res.json()) as SessionResponse;
}

export async function postTask(sessionId: string, task: string): Promise<void> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/task`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task }),
  });
  if (!res.ok) {
    throw new Error(`postTask failed: ${res.status}`);
  }
}

export async function getSession(sessionId: string): Promise<GetSessionResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`getSession failed: ${res.status}`);
  }
  return (await res.json()) as GetSessionResponse;
}

export async function getReview(sessionId: string): Promise<GetReviewResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/review`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`getReview failed: ${res.status}`);
  }
  return (await res.json()) as GetReviewResponse;
}

export async function confirmAssumption(
  sessionId: string,
  assumption_id: string,
  confirmed_value: unknown | null,
): Promise<GetReviewResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/confirm_assumption`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ assumption_id, confirmed_value }),
  });
  if (!res.ok) {
    throw new Error(`confirmAssumption failed: ${res.status}`);
  }
  return (await res.json()) as GetReviewResponse;
}

export async function cancelCallback(
  sessionId: string,
  cb_id: string,
): Promise<GetReviewResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/callbacks/${cb_id}/cancel`, {
    method: "POST",
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`cancelCallback failed: ${res.status}`);
  }
  return (await res.json()) as GetReviewResponse;
}

export async function restoreCallback(
  sessionId: string,
  cb_id: string,
): Promise<GetReviewResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/callbacks/${cb_id}/restore`, {
    method: "POST",
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`restoreCallback failed: ${res.status}`);
  }
  return (await res.json()) as GetReviewResponse;
}

export async function triggerCallback(
  sessionId: string,
  cb_id: string,
): Promise<GetReviewResponse> {
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}/callbacks/${cb_id}/trigger`, {
    method: "POST",
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`triggerCallback failed: ${res.status}`);
  }
  return (await res.json()) as GetReviewResponse;
}

export async function deleteSession(sessionId: string): Promise<void> {
  // Backend Phase D5: explicit dismissal of PostCallReview / completion.
  const res = await fetch(`${apiBaseUrl()}/api/sessions/${sessionId}`, {
    method: "DELETE",
    cache: "no-store",
  });
  if (!res.ok && res.status !== 404) {
    throw new Error(`deleteSession failed: ${res.status}`);
  }
}
