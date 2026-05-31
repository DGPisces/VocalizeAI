import React from "react";
import { useEffect, useState } from "react";

export type DeviceSelection = {
  inputId: string;
  outputId: string;
};

export function DeviceProbe({
  onSelectionChange
}: {
  onSelectionChange?: (selection: DeviceSelection) => void;
}) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [inputId, setInputId] = useState("");
  const [outputId, setOutputId] = useState("");

  useEffect(() => {
    setInputId(localStorage.getItem("vocalize.inputDeviceId") ?? "");
    setOutputId(localStorage.getItem("vocalize.outputDeviceId") ?? "");
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!navigator.mediaDevices?.enumerateDevices) {
      return;
    }
    navigator.mediaDevices.enumerateDevices().then((items) => {
      if (!cancelled) {
        setDevices(items);
      }
    }).catch(() => {
      if (!cancelled) {
        setDevices([]);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function updateSelection(next: DeviceSelection) {
    setInputId(next.inputId);
    setOutputId(next.outputId);
    localStorage.setItem("vocalize.inputDeviceId", next.inputId);
    localStorage.setItem("vocalize.outputDeviceId", next.outputId);
    onSelectionChange?.(next);
  }

  return (
    <section className="card stack" aria-label="Devices">
      <div className="card-title">Devices</div>
      <p>{devices.length ? `${devices.length} devices detected` : "Default devices"}</p>
      <label className="form-row">
        <span className="form-label">Microphone</span>
        <select
          className="form-input"
          value={inputId}
          onChange={(event) => updateSelection({ inputId: event.target.value, outputId })}
        >
          <option value="">Default microphone</option>
          {devices.filter((device) => device.kind === "audioinput").map((device) => (
            <option key={device.deviceId} value={device.deviceId}>
              {device.label || "Microphone"}
            </option>
          ))}
        </select>
      </label>
      <label className="form-row">
        <span className="form-label">Speaker</span>
        <select
          className="form-input"
          value={outputId}
          onChange={(event) => updateSelection({ inputId, outputId: event.target.value })}
        >
          <option value="">Default speaker</option>
          {devices.filter((device) => device.kind === "audiooutput").map((device) => (
            <option key={device.deviceId} value={device.deviceId}>
              {device.label || "Speaker"}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}
