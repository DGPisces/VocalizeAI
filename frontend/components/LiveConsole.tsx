import React from "react";
import { useEffect, useRef, useState } from "react";
import { BrowserAudioBridge } from "./BrowserAudioBridge";
import { DeviceProbe } from "./DeviceProbe";
import { postTask } from "../lib/api";
import { initialReadiness } from "../lib/state";
import { trustedSessionWsUrl, VocalizeSocket } from "../lib/ws";
import type { DecodedAudioFrame } from "../lib/ws";
import { isE2eAudioHookEnabled } from "../src/env";

declare global {
  interface Window {
    __vocalizeSendSyntheticPcm?: () => void;
  }
}

export function LiveConsole({
  sessionId,
  wsUrl,
  initialError
}: {
  sessionId: string;
  wsUrl: string;
  initialError?: string;
}) {
  const socketRef = useRef<VocalizeSocket | null>(null);
  const audioDiagnosticsRef = useRef<Record<string, {
    bytes: number;
    frames: number;
    reported: boolean;
  }>>({});
  const [readiness, setReadiness] = useState(initialReadiness);
  const [task, setTask] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [error, setError] = useState(initialError ?? null);
  const [playbackFrame, setPlaybackFrame] = useState<DecodedAudioFrame | null>(null);
  const [reply, setReply] = useState("");
  const [activeClarification, setActiveClarification] = useState<{
    field: string;
    question: string;
  } | null>(null);

  function connectSocket() {
    if (socketRef.current) {
      return;
    }
    if (!wsUrl) {
      setError("Missing WebSocket URL");
      return;
    }
    let trustedUrl: string;
    try {
      trustedUrl = trustedSessionWsUrl(wsUrl, sessionId);
    } catch (urlError) {
      const message = urlError instanceof Error ? urlError.message : "Invalid WebSocket URL";
      setError(message);
      return;
    }
    audioDiagnosticsRef.current = {};
    const socket = new VocalizeSocket(trustedUrl, sessionId, {
      onFrame(frame) {
        if (frame.type === "readiness_change") {
          setReadiness({
            passed: frame.passed,
            missingCritical: frame.missing_critical,
            confidence: frame.confidence
          });
        } else if (frame.type === "clarification_request") {
          setActiveClarification({
            field: frame.field,
            question: frame.question
          });
          setEvents((prev) => [...prev, `clarification: ${frame.question}`]);
        } else if (frame.type === "transcript_update") {
          setEvents((prev) => [...prev, `${frame.role}: ${frame.text}`]);
        } else if (frame.type === "state_update") {
          const eventName =
            typeof frame.diff.event === "string" ? frame.diff.event : "state_update";
          setEvents((prev) => [...prev, `state: ${eventName}`]);
        } else if (frame.type === "error") {
          setError(frame.message_zh);
        }
      },
      onAudio(audio) {
        setPlaybackFrame(audio);
        const stats = audioDiagnosticsRef.current[audio.role] ?? {
          bytes: 0,
          frames: 0,
          reported: false
        };
        stats.bytes += audio.pcm.byteLength;
        stats.frames += 1;
        audioDiagnosticsRef.current[audio.role] = stats;
        if (!stats.reported) {
          stats.reported = true;
          setEvents((prev) => [...prev, `audio:${audio.role}:${audio.pcm.byteLength}`]);
        }
      },
      onError(message) {
        setError(message);
      }
    });
    socket.connect();
    socketRef.current = socket;
  }

  useEffect(() => {
    return () => {
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!isE2eAudioHookEnabled()) {
      return;
    }
    window.__vocalizeSendSyntheticPcm = () => {
      socketRef.current?.sendAudio(new Uint8Array([1, 2, 3, 4]));
    };
    return () => {
      delete window.__vocalizeSendSyntheticPcm;
    };
  }, []);

  async function submitTask() {
    try {
      await postTask(sessionId, task);
      connectSocket();
      setEvents((prev) => [...prev, task || "task submitted"]);
    } catch (submitError) {
      const message =
        submitError instanceof Error ? submitError.message : "Task submission failed";
      setError(message);
    }
  }

  function sendReply(text: string) {
    const trimmed = text.trim();
    if (!trimmed) {
      return;
    }
    if (activeClarification) {
      socketRef.current?.send({ type: "ack_clarification", slot_value: trimmed });
      setActiveClarification(null);
    } else {
      socketRef.current?.send({ type: "text_input", text: trimmed, lang_hint: "zh" });
    }
    setEvents((prev) => [...prev, `user: ${trimmed}`]);
    setReply("");
  }

  function handover() {
    socketRef.current?.send({ type: "mode_change", mode: "call_listening" });
  }

  return (
    <main id="main" className="app-shell">
      <div className="page-frame">
        <div className="guided-console">
          <div className="stack">
            <section className="card stack">
              <div>
                <div className="card-title">Session</div>
                <h1>VocalizeAI Live</h1>
                <p>Session {sessionId}</p>
                <p className="chip">WS {wsUrl}</p>
              </div>
              {error ? <div className="alert alert--bad" role="alert">{error}</div> : null}
              <label className="form-row">
                <span className="form-label">电话任务</span>
                <input
                  className="form-input form-input--full"
                  value={task}
                  onChange={(event) => setTask(event.target.value)}
                  placeholder="帮我订今晚 7 点 4 个人的位子"
                />
              </label>
              <button
                className="btn-primary"
                type="button"
                onClick={submitTask}
              >
                提交任务
              </button>
              <label className="form-row">
                <span className="form-label">
                  {activeClarification ? "回答商家补充问题" : "输入补充信息"}
                </span>
                <input
                  className="form-input form-input--full"
                  value={reply}
                  onChange={(event) => setReply(event.target.value)}
                  placeholder={activeClarification ? "比如：没有过敏" : "比如：靠窗、不要太晚"}
                />
              </label>
              {activeClarification ? (
                <div className="alert alert--warn">
                  {activeClarification.question}
                </div>
              ) : null}
              <button
                className="btn-secondary"
                type="button"
                onClick={() => sendReply(reply)}
              >
                发送
              </button>
            </section>
            <section className="card stack" aria-label="Transcript">
              <div className="card-title">Transcript</div>
              {events.length ? (
                events.map((event, index) => <p key={`${event}-${index}`}>{event}</p>)
              ) : (
                <p>等待输入...</p>
              )}
            </section>
          </div>
          <aside className="stack rail">
            <BrowserAudioBridge
              sendAudio={(pcm) => socketRef.current?.sendAudio(pcm) ?? false}
              getBufferedAmount={() => socketRef.current?.bufferedAmount() ?? 0}
              onStatusChange={() => undefined}
              playbackFrame={playbackFrame}
            />
            <section className={readiness.passed ? "alert alert--ok" : "alert alert--warn"}>
              {readiness.passed ? "信息已足够" : "等待关键信息"}
            </section>
            <button
              className="btn-primary"
              type="button"
              disabled={!readiness.passed}
              onClick={handover}
            >
              交接
            </button>
            <DeviceProbe
              onSelectionChange={({ inputId, outputId }) => {
                socketRef.current?.send({
                  type: "set_devices",
                  input_id: inputId,
                  output_id: outputId,
                  aec: true
                });
              }}
            />
          </aside>
        </div>
      </div>
    </main>
  );
}
