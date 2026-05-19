import { defineConfig, devices } from "@playwright/test";
import Module from "node:module";
import path from "node:path";

process.env.NODE_PATH = [
  path.resolve(process.cwd(), "node_modules"),
  process.env.NODE_PATH
].filter(Boolean).join(path.delimiter);
(Module as unknown as { Module: { _initPaths: () => void } }).Module._initPaths();

export default defineConfig({
  testDir: "../tests/integration",
  timeout: 30_000,
  use: {
    baseURL: "http://localhost:3000",
    permissions: ["microphone"],
    launchOptions: {
      args: [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream"
      ]
    },
    ...devices["Desktop Chrome"]
  },
  webServer: [
    {
      command: "cd .. && . .venv/bin/activate && python tests/integration/b2_loopback_server.py",
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: !process.env.CI,
      timeout: 30_000
    },
    {
      command: "NEXT_PUBLIC_VOCALIZE_API_BASE_URL=http://127.0.0.1:8000 NEXT_PUBLIC_E2E_AUDIO_HOOK=1 npm run dev -- --hostname localhost --port 3000",
      url: "http://localhost:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 30_000
    }
  ]
});
