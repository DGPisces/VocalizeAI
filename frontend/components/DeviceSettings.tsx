import React, { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "@/src/i18n";
import { AudioLevelMeter } from "./AudioLevelMeter";

export type DevicePreferences = {
  inputId: string;
  outputId: string;
  aec: boolean;
};

export type DeviceSwitchStatus =
  | { kind: "switching" }
  | { kind: "success" }
  | { kind: "switch_error" }
  | { kind: "speaker_fallback" };

interface Props {
  autoTranslate: boolean;
  onAutoTranslateChange: (next: boolean) => void;
  devicePreferences?: DevicePreferences;
  onDevicePreferencesChange?: (preferences: DevicePreferences) => void;
  deviceSwitchStatus?: DeviceSwitchStatus | null;
}

const STORAGE_KEYS = {
  inputId: "vocalize.device.input_id",
  outputId: "vocalize.device.output_id",
  aec: "vocalize.device.aec",
} as const;

const MIC_TEST_DURATION_MS = 3000;
const SPEAKER_TEST_DURATION_SECONDS = 0.28;
const SPEAKER_TEST_SAMPLE_RATE = 44100;

function writeAscii(view: DataView, offset: number, value: string): void {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function createSpeakerTestToneBlob(): Blob {
  const sampleCount = Math.floor(SPEAKER_TEST_SAMPLE_RATE * SPEAKER_TEST_DURATION_SECONDS);
  const buffer = new ArrayBuffer(44 + sampleCount * 2);
  const view = new DataView(buffer);

  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + sampleCount * 2, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, SPEAKER_TEST_SAMPLE_RATE, true);
  view.setUint32(28, SPEAKER_TEST_SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, sampleCount * 2, true);

  for (let index = 0; index < sampleCount; index += 1) {
    const fade = Math.min(index / 900, (sampleCount - index) / 900, 1);
    const sample = Math.sin((2 * Math.PI * 880 * index) / SPEAKER_TEST_SAMPLE_RATE) * 0.24 * fade;
    view.setInt16(44 + index * 2, Math.max(-1, Math.min(1, sample)) * 0x7fff, true);
  }

  return new Blob([buffer], { type: "audio/wav" });
}

function readStoredPreferences(): DevicePreferences {
  return {
    inputId: localStorage.getItem(STORAGE_KEYS.inputId) ?? "",
    outputId: localStorage.getItem(STORAGE_KEYS.outputId) ?? "",
    aec: localStorage.getItem(STORAGE_KEYS.aec) !== "false",
  };
}

function deviceSwitchStatusKey(status: DeviceSwitchStatus): string {
  switch (status.kind) {
    case "switching":
      return "switching_status";
    case "success":
      return "switch_success";
    case "switch_error":
      return "mic_switch_failed";
    case "speaker_fallback":
      return "speaker_fallback_warning";
  }
}

function deviceSwitchStatusVariant(status: DeviceSwitchStatus): string {
  switch (status.kind) {
    case "switching":
      return "switching";
    case "success":
      return "success";
    case "switch_error":
      return "error";
    case "speaker_fallback":
      return "warning";
  }
}

export function DeviceSettings({
  autoTranslate,
  onAutoTranslateChange,
  devicePreferences,
  onDevicePreferencesChange,
  deviceSwitchStatus,
}: Props) {
  const t = useTranslations("device_settings");
  const [inputs, setInputs] = useState<MediaDeviceInfo[]>([]);
  const [outputs, setOutputs] = useState<MediaDeviceInfo[]>([]);
  const [inputId, setInputId] = useState<string>(() => devicePreferences?.inputId ?? readStoredPreferences().inputId);
  const [outputId, setOutputId] = useState<string>(() => devicePreferences?.outputId ?? readStoredPreferences().outputId);
  const [aec, setAec] = useState<boolean>(() => devicePreferences?.aec ?? readStoredPreferences().aec);
  const [recording, setRecording] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const [recordingUrl, setRecordingUrl] = useState<string | null>(null);
  const [recordingUnsupported, setRecordingUnsupported] = useState(false);
  const [permissionHint, setPermissionHint] = useState(false);
  const [localSpeakerFallback, setLocalSpeakerFallback] = useState(false);
  const recordingUrlRef = useRef<string | null>(null);
  const micTestTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const micTestStreamRef = useRef<MediaStream | null>(null);
  const micTestRecorderRef = useRef<MediaRecorder | null>(null);
  const micTestAnimationRef = useRef<number | null>(null);
  const micTestContextRef = useRef<AudioContext | null>(null);
  const micPlaybackRef = useRef<HTMLAudioElement | null>(null);

  const loadDevices = useCallback(async () => {
    const devs = await navigator.mediaDevices?.enumerateDevices();
    if (!devs) return;
    setInputs(devs.filter(d => d.kind === "audioinput"));
    setOutputs(devs.filter(d => d.kind === "audiooutput"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    navigator.mediaDevices?.enumerateDevices().then(devs => {
      if (cancelled) return;
      setInputs(devs.filter(d => d.kind === "audioinput"));
      setOutputs(devs.filter(d => d.kind === "audiooutput"));
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!devicePreferences) return;
    setInputId(devicePreferences.inputId);
    setOutputId(devicePreferences.outputId);
    setAec(devicePreferences.aec);
  }, [devicePreferences]);

  const revokeRecordingUrl = useCallback(() => {
    if (!recordingUrlRef.current) return;
    URL.revokeObjectURL?.(recordingUrlRef.current);
    recordingUrlRef.current = null;
  }, []);

  const stopMicLevel = useCallback(() => {
    if (micTestAnimationRef.current !== null) {
      cancelAnimationFrame(micTestAnimationRef.current);
      micTestAnimationRef.current = null;
    }
    const context = micTestContextRef.current;
    micTestContextRef.current = null;
    if (context) {
      void context.close();
    }
    setMicLevel(0);
  }, []);

  const stopMicTestStream = useCallback(() => {
    micTestStreamRef.current?.getTracks().forEach(track => track.stop());
    micTestStreamRef.current = null;
  }, []);

  const clearMicTestTimer = useCallback(() => {
    if (!micTestTimerRef.current) return;
    clearTimeout(micTestTimerRef.current);
    micTestTimerRef.current = null;
  }, []);

  const finishMicTest = useCallback(() => {
    clearMicTestTimer();
    stopMicLevel();
    stopMicTestStream();
    micTestRecorderRef.current = null;
    setRecording(false);
  }, [clearMicTestTimer, stopMicLevel, stopMicTestStream]);

  const startMicLevel = useCallback((stream: MediaStream) => {
    setMicLevel(0.12);
    const AudioContextCtor = window.AudioContext;
    if (!AudioContextCtor) return;

    const context = new AudioContextCtor();
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    const source = context.createMediaStreamSource(stream);
    source.connect(analyser);
    micTestContextRef.current = context;
    const data = new Uint8Array(analyser.fftSize);

    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const value of data) {
        const normalized = (value - 128) / 128;
        sum += normalized * normalized;
      }
      setMicLevel(Math.min(1, Math.sqrt(sum / data.length) * 4));
      micTestAnimationRef.current = requestAnimationFrame(tick);
    };
    tick();
  }, []);

  useEffect(() => {
    return () => {
      clearMicTestTimer();
      stopMicLevel();
      stopMicTestStream();
      revokeRecordingUrl();
    };
  }, [clearMicTestTimer, revokeRecordingUrl, stopMicLevel, stopMicTestStream]);

  function emitPreferences(preferences: DevicePreferences) {
    onDevicePreferencesChange?.(preferences);
  }

  function persistInput(id: string) {
    setInputId(id);
    localStorage.setItem(STORAGE_KEYS.inputId, id);
    emitPreferences({ inputId: id, outputId, aec });
  }

  function persistOutput(id: string) {
    setOutputId(id);
    localStorage.setItem(STORAGE_KEYS.outputId, id);
    emitPreferences({ inputId, outputId: id, aec });
  }

  function toggleAec(next: boolean) {
    setAec(next);
    localStorage.setItem(STORAGE_KEYS.aec, String(next));
    emitPreferences({ inputId, outputId, aec: next });
  }

  async function refreshDevices() {
    setPermissionHint(false);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());
    } catch {
      setPermissionHint(true);
    }
    await loadDevices();
  }

  async function startMicTest() {
    if (recording) return;

    setRecordingUnsupported(false);
    setPermissionHint(false);
    setRecordingUrl(null);
    revokeRecordingUrl();

    const audio: MediaTrackConstraints = {
      channelCount: 1,
      echoCancellation: aec,
      noiseSuppression: true,
      autoGainControl: true,
      ...(inputId ? { deviceId: { exact: inputId } } : {}),
    };

    let stream: MediaStream | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio });
      micTestStreamRef.current = stream;
      setRecording(true);
      startMicLevel(stream);

      const MediaRecorderCtor = globalThis.MediaRecorder;
      if (!MediaRecorderCtor) {
        setRecordingUnsupported(true);
        micTestTimerRef.current = setTimeout(finishMicTest, MIC_TEST_DURATION_MS);
        return;
      }

      const chunks: Blob[] = [];
      const recorder = new MediaRecorderCtor(stream);
      micTestRecorderRef.current = recorder;
      recorder.ondataavailable = event => {
        if (event.data.size > 0) {
          chunks.push(event.data);
        }
      };
      recorder.onstop = () => {
        if (chunks.length > 0) {
          const url = URL.createObjectURL(new Blob(chunks, { type: chunks[0].type || "audio/webm" }));
          recordingUrlRef.current = url;
          setRecordingUrl(url);
        }
        finishMicTest();
      };
      recorder.start();
      micTestTimerRef.current = setTimeout(() => {
        recorder.stop();
      }, MIC_TEST_DURATION_MS);
    } catch {
      if (micTestStreamRef.current === stream) {
        micTestStreamRef.current = null;
      }
      stream?.getTracks().forEach(track => track.stop());
      stopMicLevel();
      setPermissionHint(true);
      setRecording(false);
    }
  }

  async function testSpeaker() {
    setLocalSpeakerFallback(false);
    const toneUrl = URL.createObjectURL(createSpeakerTestToneBlob());
    const audio = new Audio(toneUrl);
    audio.onended = () => URL.revokeObjectURL?.(toneUrl);

    if (outputId) {
      const routedAudio = audio as HTMLAudioElement & { setSinkId?: (sinkId: string) => Promise<void> };
      if (typeof routedAudio.setSinkId === "function") {
        try {
          await routedAudio.setSinkId(outputId);
        } catch {
          setLocalSpeakerFallback(true);
        }
      } else {
        setLocalSpeakerFallback(true);
      }
    }

    try {
      await audio.play();
    } catch {
      URL.revokeObjectURL?.(toneUrl);
    }
  }

  const switching = deviceSwitchStatus?.kind === "switching";
  const statusChip = deviceSwitchStatus ? (
    <div
      role="status"
      aria-live="polite"
      className={`device-settings__status device-settings__status--${deviceSwitchStatusVariant(deviceSwitchStatus)}`}
    >
      {t(deviceSwitchStatusKey(deviceSwitchStatus))}
    </div>
  ) : null;
  const speakerStatus = deviceSwitchStatus?.kind === "speaker_fallback"
    ? statusChip
    : null;
  const localSpeakerStatus = localSpeakerFallback ? (
    <div
      role="status"
      aria-live="polite"
      className="device-settings__status device-settings__status--warning"
    >
      {t("speaker_fallback_warning")}
    </div>
  ) : null;
  const captureStatus = deviceSwitchStatus && deviceSwitchStatus.kind !== "speaker_fallback"
    ? statusChip
    : null;

  return (
    <section className="device-settings" aria-label={t("microphone")}>
      <label className="device-settings__row">
        <span>{t("microphone")}</span>
        <select
          value={inputId}
          onChange={e => persistInput(e.target.value)}
          disabled={switching || recording}
        >
          <option value="">{t("default")}</option>
          {inputs.map(d => (
            <option key={d.deviceId} value={d.deviceId}>{d.label || d.deviceId}</option>
          ))}
        </select>
      </label>
      <label className="device-settings__row">
        <span>{t("speaker")}</span>
        <select value={outputId} onChange={e => persistOutput(e.target.value)}>
          <option value="">{t("default")}</option>
          {outputs.map(d => (
            <option key={d.deviceId} value={d.deviceId}>{d.label || d.deviceId}</option>
          ))}
        </select>
      </label>
      {speakerStatus}
      {localSpeakerStatus}
      <div className="device-settings__actions device-settings__actions--device-refresh">
        <button type="button" className="chip-btn" onClick={refreshDevices}>
          {t("refresh_devices")}
        </button>
      </div>
      {permissionHint ? (
        <p className="device-settings__hint">{t("permission_hint")}</p>
      ) : null}
      <label className="device-settings__row">
        <span>{t("aec")}</span>
        <input
          type="checkbox"
          checked={aec}
          onChange={e => toggleAec(e.target.checked)}
          disabled={switching || recording}
        />
      </label>
      {captureStatus}
      <label className="device-settings__row">
        <span>{t("auto_translate")}</span>
        <input
          type="checkbox"
          checked={autoTranslate}
          onChange={e => onAutoTranslateChange(e.target.checked)}
        />
      </label>
      <div className="device-settings__section">
        <div className="device-settings__actions">
          <button
            type="button"
            className="chip-btn chip-btn--primary"
            onClick={startMicTest}
            disabled={recording}
          >
            {t("test_microphone")}
          </button>
          {recordingUrl ? (
            <button
              type="button"
              className="chip-btn chip-btn--primary"
              onClick={() => void micPlaybackRef.current?.play()}
            >
              {t("play_recording")}
            </button>
          ) : null}
        </div>
        {recording ? (
          <div className="device-settings__meter">
            <div
              role="status"
              aria-live="polite"
              className="device-settings__status device-settings__status--switching"
            >
              {t("recording_status")}
            </div>
            <AudioLevelMeter level={micLevel} label="Microphone level" />
          </div>
        ) : null}
        {recordingUnsupported ? (
          <div
            role="status"
            aria-live="polite"
            className="device-settings__status device-settings__status--warning"
          >
            {t("recording_unsupported")}
          </div>
        ) : null}
        {recordingUrl ? (
          <audio ref={micPlaybackRef} src={recordingUrl} controls />
        ) : null}
      </div>
      <div className="device-settings__section">
        <div className="device-settings__actions">
          <button type="button" className="chip-btn chip-btn--primary" onClick={testSpeaker}>
            {t("test_speaker")}
          </button>
        </div>
      </div>
    </section>
  );
}
