import React, { useMemo, useState } from "react";
import {
  Activity,
  CheckCircle2,
  CircleDot,
  Headphones,
  Languages,
  MessageSquare,
  Mic,
  PhoneCall,
  RotateCcw,
  Settings2,
  SlidersHorizontal,
  Stethoscope,
} from "lucide-react";

import { LanguageToggle } from "../components/LanguageToggle";

type Locale = "zh" | "en";
type PreviewMode = "plan" | "ready" | "live" | "review";

interface PreviewCopy {
  subtitle: string;
  status: string;
  preview: string;
  taskTitle: string;
  taskValue: string;
  taskHint: string;
  tabs: Record<PreviewMode, string>;
  readiness: string;
  missing: string;
  ready: string;
  handover: string;
  handoverSteps: string[];
  preflight: string;
  transcript: string;
  clarification: string;
  supplement: string;
  takeover: string;
  hangup: string;
  review: string;
  settings: string;
  diagnostics: string;
  callbacks: string;
  assumptions: string;
  turns: string;
  autoTranslate: string;
  mic: string;
  speaker: string;
  doctor: string;
  reset: string;
  previewOnly: string;
}

const modeOrder: PreviewMode[] = ["plan", "ready", "live", "review"];

export function PreviewConsole({ locale }: { locale: Locale }) {
  const copy = useMemo(() => previewCopy(locale), [locale]);
  const [mode, setMode] = useState<PreviewMode>("ready");
  const [showClarification, setShowClarification] = useState(true);
  const [takeoverActive, setTakeoverActive] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(true);
  const readiness = mode === "plan" ? 64 : 100;

  return (
    <main id="main" className="focused-shell preview-page">
      <header className="focused-header preview-page__header">
        <a className="focused-brand" href={`/${locale}/`} aria-label="VocalizeAI home">
          <span className="focused-brand__mark">V</span>
          <span>
            <strong>VocalizeAI</strong>
            <small>{copy.subtitle}</small>
          </span>
        </a>
        <span className="focused-status">{copy.status}</span>
        <nav className="focused-header__actions" aria-label="Preview controls">
          <LanguageToggle />
          <button
            type="button"
            className="focused-icon-button"
            aria-label={copy.doctor}
          >
            <Stethoscope aria-hidden size={17} strokeWidth={2} />
          </button>
          <button
            type="button"
            className="focused-icon-button"
            aria-label={copy.settings}
            onClick={() => setSettingsOpen((value) => !value)}
          >
            <Settings2 aria-hidden size={17} strokeWidth={2} />
          </button>
        </nav>
      </header>

      <section className="preview-page__modebar" aria-label={copy.preview}>
        <div className="preview-page__tabs" role="tablist">
          {modeOrder.map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={mode === item}
              className={mode === item ? "is-active" : ""}
              onClick={() => setMode(item)}
            >
              {copy.tabs[item]}
            </button>
          ))}
        </div>
        <span>{copy.previewOnly}</span>
      </section>

      <section className="focused-hero preview-page__hero">
        <div className="focused-task">
          <h1>{copy.taskTitle}</h1>
          <div className="preview-task-input" aria-label="Task input preview">
            <span>{copy.taskValue}</span>
            <button type="button" className="focused-button focused-button--primary">
              <PhoneCall aria-hidden size={17} strokeWidth={2} />
              {copy.handover}
            </button>
          </div>
          <p className="preview-muted">{copy.taskHint}</p>
        </div>
        <aside className="focused-readiness" aria-label={copy.readiness}>
          <div className="focused-panel-heading">
            <span>
              <Activity aria-hidden size={17} strokeWidth={2} />
              {copy.readiness}
            </span>
            <strong>{readiness}%</strong>
          </div>
          <div className="preview-meter" aria-hidden>
            <span style={{ width: `${readiness}%` }} />
          </div>
          <div className="preview-status-list">
            <StatusRow ok={readiness === 100} label={readiness === 100 ? copy.ready : copy.missing} />
            <StatusRow ok label="LLM" />
            <StatusRow ok label={copy.mic} />
          </div>
        </aside>
      </section>

      <div className="preview-grid">
        <section className="preview-panel preview-panel--wide">
          <PanelTitle icon={<MessageSquare />} label={copy.preflight} />
          <div className="preview-chat">
            <Message role="system" text={locale === "en" ? "Need party size and time." : "还缺人数和时间。"} />
            <Message role="user" text={locale === "en" ? "Four people, 7 pm tonight." : "今晚 7 点，4 个人。"} />
            <Message role="system" text={locale === "en" ? "Ready." : "可以开始。"} />
          </div>
          <div className="preview-inline-form">
            <input value={locale === "en" ? "Window seat if possible" : "尽量靠窗"} readOnly />
            <button type="button">{copy.supplement}</button>
          </div>
        </section>

        <section className="preview-panel">
          <PanelTitle icon={<PhoneCall />} label={copy.handover} />
          <ol className="preview-steps">
            {copy.handoverSteps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <button type="button" className="focused-button focused-button--primary">
            {copy.handover}
          </button>
        </section>

        <section className="preview-panel preview-panel--wide">
          <PanelTitle icon={<Headphones />} label={copy.transcript} />
          <div className="preview-transcript">
            <TranscriptRow side="user" label={locale === "en" ? "You" : "你"} text={locale === "en" ? "I need a reservation for tonight." : "我想订今晚的位置。"} />
            <TranscriptRow side="merchant" label={locale === "en" ? "Merchant" : "商家"} text={locale === "en" ? "What time and how many guests?" : "几点？几位？"} />
            <TranscriptRow side="assistant" label="VocalizeAI" text={locale === "en" ? "7 pm, four guests, window seat if available." : "今晚 7 点，4 位，有靠窗位优先。"} />
          </div>
          <div className="preview-call-actions">
            <button
              type="button"
              className={takeoverActive ? "focused-button focused-button--dark" : "focused-button"}
              onClick={() => setTakeoverActive((value) => !value)}
            >
              <Mic aria-hidden size={17} strokeWidth={2} />
              {copy.takeover}
            </button>
            <button type="button" className="focused-button">
              <Languages aria-hidden size={17} strokeWidth={2} />
              {copy.autoTranslate}
            </button>
            <button type="button" className="focused-button">
              {copy.hangup}
            </button>
          </div>
        </section>

        <section className="preview-panel">
          <PanelTitle icon={<CircleDot />} label={copy.clarification} />
          {showClarification ? (
            <div className="preview-clarification">
              <strong>{locale === "en" ? "Window seat unavailable." : "没有靠窗位。"}</strong>
              <span>{locale === "en" ? "Accept a standard table?" : "普通座可以吗？"}</span>
              <div>
                <button type="button" onClick={() => setShowClarification(false)}>
                  {locale === "en" ? "Accept" : "可以"}
                </button>
                <button type="button" onClick={() => setShowClarification(false)}>
                  {locale === "en" ? "Decline" : "不行"}
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              className="focused-button"
              onClick={() => setShowClarification(true)}
            >
              {copy.reset}
            </button>
          )}
        </section>

        <section className="preview-panel preview-panel--wide">
          <PanelTitle icon={<CheckCircle2 />} label={copy.review} />
          <p className="preview-result">
            {locale === "en"
              ? "Reservation confirmed for four people at 7:00 pm."
              : "已订今晚 7 点，4 位。"}
          </p>
          <div className="focused-metric-list">
            <span>{copy.assumptions}<strong>1</strong></span>
            <span>{copy.callbacks}<strong>0</strong></span>
            <span>{copy.turns}<strong>6</strong></span>
          </div>
        </section>

        <section className="preview-panel">
          <PanelTitle icon={<SlidersHorizontal />} label={copy.settings} />
          {settingsOpen ? (
            <dl className="focused-diagnostics">
              <div>
                <dt>{copy.mic}</dt>
                <dd>Built-in</dd>
              </div>
              <div>
                <dt>{copy.speaker}</dt>
                <dd>Default</dd>
              </div>
              <div>
                <dt>{copy.autoTranslate}</dt>
                <dd>{locale === "en" ? "On" : "开"}</dd>
              </div>
            </dl>
          ) : (
            <button type="button" className="focused-button" onClick={() => setSettingsOpen(true)}>
              {copy.settings}
            </button>
          )}
        </section>
      </div>
    </main>
  );
}

function PanelTitle({ icon, label }: { icon: React.ReactElement; label: string }) {
  return (
    <div className="focused-panel-heading">
      <span>
        {React.cloneElement(icon, { "aria-hidden": true, size: 17, strokeWidth: 2 })}
        {label}
      </span>
    </div>
  );
}

function StatusRow({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={ok ? "is-ok" : "is-warn"}>
      {label}
    </span>
  );
}

function Message({ role, text }: { role: "system" | "user"; text: string }) {
  return (
    <div className={`preview-message preview-message--${role}`}>
      {text}
    </div>
  );
}

function TranscriptRow({
  side,
  label,
  text,
}: {
  side: "user" | "merchant" | "assistant";
  label: string;
  text: string;
}) {
  return (
    <div className={`preview-transcript-row preview-transcript-row--${side}`}>
      <strong>{label}</strong>
      <span>{text}</span>
    </div>
  );
}

function previewCopy(locale: Locale): PreviewCopy {
  if (locale === "en") {
    return {
      subtitle: "Frontend preview",
      status: "No backend",
      preview: "Preview",
      taskTitle: "Frontend states",
      taskValue: "Book a table tonight at 7:00 for four people",
      taskHint: "Mock data only. Use this page to review layout and interactions.",
      tabs: {
        plan: "Plan",
        ready: "Ready",
        live: "Live",
        review: "Review",
      },
      readiness: "Readiness",
      missing: "Party size missing",
      ready: "Ready",
      handover: "Handover",
      handoverSteps: ["Open speakerphone", "Place phone near Mac", "Start handover"],
      preflight: "Preflight",
      transcript: "Transcript",
      clarification: "Clarification",
      supplement: "Add note",
      takeover: "Take over",
      hangup: "Hang up",
      review: "Result",
      settings: "Settings",
      diagnostics: "Diagnostics",
      callbacks: "Callbacks",
      assumptions: "Checks",
      turns: "Turns",
      autoTranslate: "Translate",
      mic: "Microphone",
      speaker: "Speaker",
      doctor: "Doctor",
      reset: "Show prompt",
      previewOnly: "Static UI preview",
    };
  }
  return {
    subtitle: "前端预览",
    status: "不连接后端",
    preview: "预览",
    taskTitle: "前端状态预览",
    taskValue: "今晚 7 点，4 位，尽量靠窗",
    taskHint: "只看界面和交互，不创建真实会话。",
    tabs: {
      plan: "规划",
      ready: "就绪",
      live: "通话",
      review: "复盘",
    },
    readiness: "准备",
    missing: "缺少人数",
    ready: "就绪",
    handover: "交接",
    handoverSteps: ["打开扬声器", "手机靠近 Mac", "开始交接"],
    preflight: "预沟通",
    transcript: "通话记录",
    clarification: "补充确认",
    supplement: "补充",
    takeover: "接话",
    hangup: "挂断",
    review: "结果",
    settings: "设置",
    diagnostics: "诊断",
    callbacks: "回拨",
    assumptions: "核对项",
    turns: "轮次",
    autoTranslate: "翻译",
    mic: "麦克风",
    speaker: "扬声器",
    doctor: "诊断",
    reset: "重新显示",
    previewOnly: "静态预览",
  };
}
