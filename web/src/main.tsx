import { StrictMode, Suspense, lazy } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { isAuthFrame } from "./auth";

// 運営者の画面(/ops。§15.1 の M9)は、別のファイルに分けて、/ops を開いたときだけ読み込む
const OpsApp = lazy(() => import("./ops/OpsApp").then((m) => ({ default: m.OpsApp })));
// ペルソナデザイナ(/designer。§15.1 の M10)も、開いたときだけ読み込む
const DesignerApp = lazy(() => import("./designer/DesignerApp").then((m) => ({ default: m.DesignerApp })));
const at = (prefix: string) => window.location.pathname === prefix || window.location.pathname.startsWith(`${prefix}/`);
const isOps = at("/ops");
const isDesigner = at("/designer");

// 画面を出さない取り直し(見えない枠)の戻り先では、アプリを動かさない(元の画面が、枠の URL から結果を読み取る)。
// リダイレクトでサインインから戻ってきたタブでは、アプリを動かし、起動時に結果を受け取る
if (!isAuthFrame()) {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      {isOps || isDesigner ? (
        <Suspense fallback={<p aria-busy="true">読み込んでいます…</p>}>
          {isOps ? <OpsApp /> : <DesignerApp />}
        </Suspense>
      ) : (
        <App />
      )}
    </StrictMode>,
  );
}
