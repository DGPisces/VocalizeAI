import { defineConfig } from "vite";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 3000,
  },
  preview: {
    host: "127.0.0.1",
    port: 3000,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
