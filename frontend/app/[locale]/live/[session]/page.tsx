import React from "react";
import { unstable_setRequestLocale } from "next-intl/server";
import { LivePageClient } from "./LivePageClient";

interface Props {
  params: { locale: string; session: string };
  searchParams?: { ws?: string; debug?: string };
}

export default function LivePage({ params, searchParams }: Props) {
  unstable_setRequestLocale(params.locale);
  return (
    <LivePageClient
      locale={params.locale}
      sessionId={params.session}
      initialWsUrl={searchParams?.ws}
      debug={searchParams?.debug === "1"}
    />
  );
}
