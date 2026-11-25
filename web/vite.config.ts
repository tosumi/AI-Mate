/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

// 画面の版(配信のたびに変わる目印。§15.1 の M10 の手順1)。CI ではコミットの SHA、手元ではビルドの時刻
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {};
const APP_VERSION = (env.SHA ?? "").slice(0, 12) || `local-${Date.now()}`;

/** ビルドの結果に version.json(いまの版)を入れる。開いたままの画面が、新しい版の配信に気づくために読む。 */
function versionFile(): Plugin {
  return {
    name: "mycompanion-version-file",
    apply: "build",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "version.json", source: JSON.stringify({ version: APP_VERSION }) + "\n" });
    },
  };
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), versionFile()],
  define: { __APP_VERSION__: JSON.stringify(APP_VERSION) },
  test: {
    // 単体テストは src/ だけ(e2e/ は Playwright で動かす)
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
