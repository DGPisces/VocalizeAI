import React from "react";

export function AudioLevelMeter({ level, label }: { level: number; label: string }) {
  const bars = Array.from({ length: 20 }, (_, index) => {
    const active = index / 20 <= Math.max(0, Math.min(1, level));
    return (
      <span
        className="meter-bar"
        key={index}
        style={{ opacity: active ? 1 : 0.18, height: `${Math.max(4, (index + 1) * 1.4)}px` }}
      />
    );
  });

  return (
    <div aria-label={label}>
      <div className="meter-bars" aria-hidden="true">{bars}</div>
    </div>
  );
}
