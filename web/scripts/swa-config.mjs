// Static Web Apps の設定(staticwebapp.config.json)を、ビルドのあとに dist/ へ書き出す(本体仕様書 §15.1 の M7)。
// 接続してよい先を、API とサインインの URL だけに限る(CSP)。値は、ビルドと同じ環境変数(VITE_*)から読む。
//
//   node scripts/swa-config.mjs

import { writeFileSync } from "node:fs";

const api = new URL(required("VITE_API_URL")).origin;
const login = new URL(required("VITE_ENTRA_AUTHORITY")).origin;

const csp = [
  "default-src 'self'",
  "script-src 'self'",
  "worker-src 'self'", // 通知を受け取る Service Worker(/sw.js)
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self' ${api} ${login}`,
  // サインインの状態の確認(画面を出さない取り直し)に、見えない枠を使う。枠はサインインの URL を開き、最後に自分のサイトへ戻る
  `frame-src 'self' ${login}`,
  "frame-ancestors 'self'", // 自分のサイトの見えない枠(サインインの戻り先)だけを許す。ほかのサイトには埋め込ませない
  "base-uri 'self'",
  `form-action 'self' ${login}`,
  "object-src 'none'",
].join("; ");

const config = {
  navigationFallback: { rewrite: "/index.html", exclude: ["/assets/*", "/sw.js"] },
  // Service Worker は、古いものが残らないように、キャッシュさせない
  routes: [
    { route: "/sw.js", headers: { "Cache-Control": "no-cache" } },
    // 画面の入口は、毎回新しい版かを確かめさせる(配信のあとに、古い画面が残らないように)。assets/ は名前に版が入る
    { route: "/", headers: { "Cache-Control": "no-cache" } },
    { route: "/index.html", headers: { "Cache-Control": "no-cache" } },
  ],
  globalHeaders: {
    "Content-Security-Policy": csp,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000",
  },
};

writeFileSync("dist/staticwebapp.config.json", JSON.stringify(config, null, 2) + "\n");
console.log("dist/staticwebapp.config.json を書き出しました");

function required(name) {
  const value = process.env[name];
  if (!value) {
    console.error(`環境変数 ${name} がありません`);
    process.exit(1);
  }
  return value;
}
