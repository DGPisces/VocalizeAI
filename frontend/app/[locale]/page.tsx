import Link from "next/link";
import React from "react";
import { unstable_setRequestLocale } from "next-intl/server";

interface Props {
  params: { locale: string };
}

export default function HomePage({ params }: Props) {
  unstable_setRequestLocale(params.locale);
  return (
    <main id="main" className="app-shell">
      <div className="page-frame stack">
        <section className="card stack">
          <h1>VocalizeAI</h1>
          <p>Browser audio bridge for AI phone tasks.</p>
          <Link className="btn-primary" href={`/${params.locale}/new`}>开始预订 / Start</Link>
        </section>
      </div>
    </main>
  );
}
