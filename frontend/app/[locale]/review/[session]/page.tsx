import React from "react";
import { unstable_setRequestLocale } from "next-intl/server";
import { ReviewPageClient } from "./ReviewPageClient";

interface Props {
  params: { locale: string; session: string };
}

export default function ReviewPage({ params }: Props) {
  unstable_setRequestLocale(params.locale);
  return (
    <ReviewPageClient
      locale={params.locale}
      sessionId={params.session}
    />
  );
}
