import React, { useCallback, useEffect, useState } from "react";
import { useRouter } from "@/src/router";
import { useTranslations } from "@/src/i18n";

import {
  cancelCallback as defaultCancelCallback,
  confirmAssumption as defaultConfirmAssumption,
  createSession as defaultCreateSession,
  deleteSession as defaultDeleteSession,
  getReview as defaultGetReview,
  restoreCallback as defaultRestoreCallback,
  triggerCallback as defaultTriggerCallback,
  type GetReviewResponse,
  type SessionResponse,
} from "../../../../lib/api";
import { PostCallReview } from "../../../../components/PostCallReview";

export interface ReviewApiClient {
  getReview: (sessionId: string) => Promise<GetReviewResponse>;
  confirmAssumption: (
    sessionId: string,
    assumption_id: string,
    confirmed_value: unknown | null,
  ) => Promise<GetReviewResponse>;
  cancelCallback: (sessionId: string, cb_id: string) => Promise<GetReviewResponse>;
  restoreCallback: (sessionId: string, cb_id: string) => Promise<GetReviewResponse>;
  triggerCallback: (sessionId: string, cb_id: string) => Promise<GetReviewResponse>;
  deleteSession: (sessionId: string) => Promise<void>;
  createSession: () => Promise<Pick<SessionResponse, "session_id">>;
}

const DEFAULT_API_CLIENT: ReviewApiClient = {
  getReview: defaultGetReview,
  confirmAssumption: defaultConfirmAssumption,
  cancelCallback: defaultCancelCallback,
  restoreCallback: defaultRestoreCallback,
  triggerCallback: defaultTriggerCallback,
  deleteSession: defaultDeleteSession,
  createSession: defaultCreateSession,
};

interface Props {
  locale: string;
  sessionId: string;
  apiClient?: ReviewApiClient;
}

export function ReviewPageClient({ locale, sessionId, apiClient }: Props) {
  const t = useTranslations();
  const router = useRouter();
  const api = apiClient ?? DEFAULT_API_CLIENT;
  const [review, setReview] = useState<GetReviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getReview(sessionId)
      .then((result) => {
        if (cancelled) return;
        setReview(result);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [api, sessionId]);

  const replaceFrom = useCallback(
    async (operation: () => Promise<GetReviewResponse>) => {
      try {
        setReview(await operation());
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  const onStartNewCall = useCallback(async () => {
    try {
      await api.deleteSession(sessionId);
    } catch {
      // The session may already be gone; creating a fresh one is still safe.
    }
    const next = await api.createSession();
    router.push(`/${locale}/live/${next.session_id}`);
  }, [api, locale, router, sessionId]);

  if (error) {
    return (
      <main id="main" className="app-shell">
        <div className="alert alert--bad" role="alert">
          {t("errors.unknown")}
        </div>
      </main>
    );
  }

  if (review === null) {
    return (
      <main id="main" className="app-shell">
        <p className="post-call-review post-call-review--empty">
          {t("post_call_review.loading")}
        </p>
      </main>
    );
  }

  return (
    <main id="main" className="app-shell">
      <PostCallReview
        assumptions={review.uncertain_assumptions}
        callbacks={review.pending_callbacks}
        call_segments={review.call_segments}
        status={review.status}
        completion_summary={review.completion_summary}
        onConfirmAssumption={(assumption_id, confirmed_value) => {
          void replaceFrom(() =>
            api.confirmAssumption(sessionId, assumption_id, confirmed_value)
          );
        }}
        onCancelCallback={(cb_id) => {
          void replaceFrom(() => api.cancelCallback(sessionId, cb_id));
        }}
        onRestoreCallback={(cb_id) => {
          void replaceFrom(() => api.restoreCallback(sessionId, cb_id));
        }}
        onTriggerCallback={(cb_id) => {
          void replaceFrom(() => api.triggerCallback(sessionId, cb_id));
        }}
        onStartNewCall={onStartNewCall}
      />
    </main>
  );
}
