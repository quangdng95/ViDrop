import { readFileSync } from "fs";
import path from "path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Mirrors vite.config.ts's define - see that file's comment for why.
const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "./package.json"), "utf-8"));

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    globals: true,
  },
});
