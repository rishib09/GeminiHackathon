import os from "node:os";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  cacheDir: path.join(os.tmpdir(), "recipe-workbench-vite-cache"),
  optimizeDeps: {
    include: [
      "react",
      "react-dom",
      "@tiptap/core",
      "@tiptap/pm/state",
      "@tiptap/pm/view",
      "@tiptap/react",
      "@tiptap/starter-kit",
      "@tiptap/extension-placeholder",
    ],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
