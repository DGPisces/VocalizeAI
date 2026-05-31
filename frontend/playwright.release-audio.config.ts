import { defineConfig, devices } from "@playwright/test";
import Module from "node:module";
import path from "node:path";

process.env.NODE_PATH = [
  path.resolve(process.cwd(), "node_modules"),
  process.env.NODE_PATH,
]
  .filter(Boolean)
  .join(path.delimiter);
(
  Module as unknown as { Module: { _initPaths: () => void } }
).Module._initPaths();

const backendURL =
  process.env.VOCALIZE_RELEASE_AUDIO_BACKEND_URL ??
  process.env.VITE_VOCALIZE_API_BASE_URL;
const frontendURL =
  process.env.VOCALIZE_RELEASE_AUDIO_FRONTEND_URL ?? "http://localhost:3000";

if (!backendURL) {
  throw new Error("VOCALIZE_RELEASE_AUDIO_BACKEND_URL is required");
}

export default defineConfig({
  testDir: "../tests/integration",
  timeout: 180_000,
  projects: [
    {
      name: "release-audio",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: frontendURL,
        permissions: ["microphone"],
        launchOptions: {
          args: [],
        },
      },
    },
  ],
  webServer: [
    {
      command: "npm run dev -- --host localhost --port 3000",
      url: frontendURL,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...process.env,
        VITE_VOCALIZE_API_BASE_URL: backendURL,
        VITE_E2E_AUDIO_HOOK: "1",
      },
    },
  ],
});
