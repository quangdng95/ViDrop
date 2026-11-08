import { readFileSync } from "fs";
import path from "path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import inlineIcons from "./plugins/inlineIcons.ts";

// The single source of truth for the app's version (see CLAUDE.md: "Version lives ONLY in
// frontend/package.json"). Baked in at build time so the running app can show the user which
// version they actually have, instead of that only being knowable from the filename/DMG.
const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "./package.json"), "utf-8"));

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [react(), tailwindcss(), inlineIcons()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      // All /api requests are forwarded to the Flask backend running on :5001.
      // changeOrigin rewrites the Host header to 127.0.0.1:5001 so that
      // Flask's is_local_request() always recognises the dev-server as local,
      // enabling download, check-link, clipboard, browse-folder, etc. — the
      // exact same feature-set as the packaged .app.
      "/api": {
        target: "http://127.0.0.1:5001",
        changeOrigin: true,
      },
    },
  },
});
