/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // 単体テストは src/ だけ(e2e/ は Playwright で動かす)
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
