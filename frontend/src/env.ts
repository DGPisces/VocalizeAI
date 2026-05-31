type ImportMetaWithEnv = ImportMeta & {
  env?: Record<string, string | undefined>;
};

export type PublicEnvKey =
  | "VOCALIZE_API_BASE_URL"
  | "VOCALIZE_WS_BASE_URL"
  | "E2E_AUDIO_HOOK";

export function readPublicEnv(key: PublicEnvKey): string | undefined {
  const viteValue = (import.meta as ImportMetaWithEnv).env?.[`VITE_${key}`];
  if (viteValue) {
    return viteValue;
  }
  if (typeof process !== "undefined") {
    return process.env?.[`VITE_${key}`];
  }
  return undefined;
}

export function readRuntimeMode(): string {
  const mode = (import.meta as ImportMetaWithEnv).env?.MODE;
  if (mode) {
    return mode;
  }
  if (typeof process !== "undefined") {
    return process.env?.NODE_ENV ?? "development";
  }
  return "development";
}

export function isE2eAudioHookEnabled(): boolean {
  return readPublicEnv("E2E_AUDIO_HOOK") === "1";
}
