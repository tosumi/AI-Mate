import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { isAuthFrame } from "./auth";

// 画面を出さない取り直し(見えない枠)の戻り先では、アプリを動かさない(元の画面が、枠の URL から結果を読み取る)。
// リダイレクトでサインインから戻ってきたタブでは、アプリを動かし、起動時に結果を受け取る
if (!isAuthFrame()) {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
