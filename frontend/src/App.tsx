import React from "react";
import { Eye, Play, Settings2, Stethoscope } from "lucide-react";

import { CreateSessionClient } from "../app/[locale]/new/CreateSessionClient";
import { LivePageClient } from "../app/[locale]/live/[session]/LivePageClient";
import { ReviewPageClient } from "../app/[locale]/review/[session]/ReviewPageClient";
import { LanguageToggle } from "../components/LanguageToggle";
import { I18nProvider } from "./i18n";
import { PreviewConsole } from "./PreviewConsole";
import { usePathname } from "./router";

type Locale = "zh" | "en";

interface RouteState {
  locale: Locale;
  view: "home" | "new" | "live" | "review" | "preview";
  sessionId?: string;
}

export function App() {
  const pathname = usePathname();
  const route = parseRoute(pathname);

  return (
    <I18nProvider locale={route.locale}>
      {route.view === "new" ? <CreateSessionClient /> : null}
      {route.view === "live" && route.sessionId ? (
        <LivePageClient locale={route.locale} sessionId={route.sessionId} />
      ) : null}
      {route.view === "review" && route.sessionId ? (
        <ReviewPageClient locale={route.locale} sessionId={route.sessionId} />
      ) : null}
      {route.view === "preview" ? <PreviewConsole locale={route.locale} /> : null}
      {route.view === "home" ? <HomeConsole locale={route.locale} /> : null}
    </I18nProvider>
  );
}

function HomeConsole({ locale }: { locale: Locale }) {
  const startHref = `/${locale}/new`;
  const previewHref = `/${locale}/preview`;
  const copy = homeCopy(locale);
  return (
    <main id="main" className="focused-shell">
      <header className="focused-header">
        <a className="focused-brand" href={`/${locale}/`} aria-label="VocalizeAI home">
          <span className="focused-brand__mark">V</span>
          <span>
            <strong>VocalizeAI</strong>
            <small>{copy.subtitle}</small>
          </span>
        </a>
        <span className="focused-status">{copy.status}</span>
        <nav className="focused-header__actions" aria-label="Console actions">
          <LanguageToggle />
          <a className="focused-button focused-button--ghost" href={previewHref}>
            <Eye aria-hidden size={17} strokeWidth={2} />
            {copy.preview}
          </a>
          <a className="focused-button focused-button--dark" href={startHref}>
            <Stethoscope aria-hidden size={17} strokeWidth={2} />
            {copy.doctor}
          </a>
          <a className="focused-button focused-button--ghost" href={startHref}>
            <Settings2 aria-hidden size={17} strokeWidth={2} />
            {copy.settings}
          </a>
        </nav>
      </header>
      <section className="focused-hero">
        <div>
          <h1>{copy.title}</h1>
          <div className="focused-task-row">
            <span>{copy.taskHint}</span>
            <a className="focused-button focused-button--primary" href={startHref}>
              <Play aria-hidden size={17} strokeWidth={2} />
              {copy.start}
            </a>
          </div>
        </div>
        <aside className="focused-readiness" aria-label="Readiness">
          <div className="focused-panel-heading">
            <span>{copy.readiness}</span>
            <strong>3/3</strong>
          </div>
          <div className="focused-chip-row">
            <span className="chip">{copy.llm}</span>
            <span className="chip">{copy.speech}</span>
            <span className="chip">{copy.env}</span>
          </div>
        </aside>
      </section>
    </main>
  );
}

function parseRoute(pathname: string): RouteState {
  const parts = pathname.split("/").filter(Boolean);
  const locale: Locale = parts[0] === "en" ? "en" : "zh";
  const view = parts[1];
  if (view === "new") {
    return { locale, view: "new" };
  }
  if (view === "live" && parts[2]) {
    return { locale, view: "live", sessionId: decodeURIComponent(parts[2]) };
  }
  if (view === "review" && parts[2]) {
    return { locale, view: "review", sessionId: decodeURIComponent(parts[2]) };
  }
  if (view === "preview") {
    return { locale, view: "preview" };
  }
  return { locale, view: "home" };
}

function homeCopy(locale: Locale) {
  if (locale === "en") {
    return {
      subtitle: "Local speech · LLM",
      status: ".env ready",
      preview: "Preview",
      doctor: "Doctor",
      settings: "Settings",
      title: "Enter a task. Start the call.",
      taskHint: "Book a table, check a status, change an appointment.",
      start: "Start session",
      readiness: "Readiness",
      llm: "LLM",
      speech: "Speech",
      env: "Config",
    };
  }
  return {
    subtitle: "本机语音 · LLM",
    status: ".env 就绪",
    preview: "预览界面",
    doctor: "诊断",
    settings: "设置",
    title: "输入任务，开始通话。",
    taskHint: "订位、查状态、改预约，直接写清条件。",
    start: "开始会话",
    readiness: "准备状态",
    llm: "LLM",
    speech: "语音",
    env: "配置",
  };
}
