import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { isAuthFrame } from "./auth";

// 運営者の画面(/ops。§15.1 の M9)は、別のファイルに分けて、/ops を開いたときだけ読み込む
const OpsApp = lazy(() => import("./ops/OpsApp").then((m) => ({ default: m.OpsApp })));
const isOps = window.location.pathname === "/ops" || window.location.pathname.startsWith("/ops/");

// 画面を出さない取り直し(見えない枠)の戻り先では、アプリを動かさない(元の画面が、枠の URL から結果を読み取る)。
// リダイレクトでサインインから戻ってきたタブでは、アプリを動かし、起動時に結果を受け取る
if (!isAuthFrame()) {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      {isOps ? (
        <Suspense fallback={<p aria-busy="true">読み込んでいます…</p>}>
          <OpsApp />
        </Suspense>
      ) : (
        <App />
      )}
    </StrictMode>,
  );
}
