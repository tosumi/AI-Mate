// 入口のタイトル画面(/。本体仕様書 §15.1 の M11 の手順1)。1枚絵の上に、「チャット」と「デザイナ」の入口を置く。
// - サインインは、それぞれの画面で行う(この画面では、サインインを求めない)。
// - 一般ユーザーのサインインの戻り先は / なので、戻ってきたときは、ここで結果を受け取り、サインインを始めた画面(/chat・/designer)へ移る。
// - 運営者の画面(/ops)への入口は置かない。
// - 絵は web/public/ の title.webp / title.png / title.jpg があれば、それを使う(なければ仮の絵 title.svg)。

import { useEffect, useMemo, useState } from "react";
import { createAuth } from "../auth";
import { loadConfig } from "../config";
import "./title.css";

const ART = ["/title.webp", "/title.png", "/title.jpg", "/title.svg"];

/** サインインの画面から戻ってきたところか(URL に、認可の結果が付いている)。 */
function isSignInReturn(): boolean {
  return /(?:^|[#&?])(code|error)=/.test(window.location.hash + window.location.search);
}

export function TitleApp() {
  const returning = useMemo(isSignInReturn, []);
  const [art, setArt] = useState(0);
  const [message, setMessage] = useState("");

  useEffect(() => {
    document.title = "FureAI ペルソナ";
    if (!returning) return;
    // サインインの結果を受け取る(MSAL が、サインインを始めた画面へ移す)
    try {
      void createAuth(loadConfig()).start().catch(() => setMessage("サインインを完了できませんでした。もう一度お試しください。"));
    } catch (e) {
      setMessage((e as Error).message);
    }
  }, [returning]);

  if (returning && !message) return <p className="title-busy" aria-busy="true">サインインしています…</p>;
  return (
    <main className="title">
      <img className="title-art" src={ART[art]} alt="" aria-hidden="true" onError={() => setArt((i) => Math.min(i + 1, ART.length - 1))} />
      <div className="title-panel">
        <h1 className="title-name">FureAI ペルソナ</h1>
        <p className="title-lead">あなたの気持ちに寄り添う、AI の話し相手</p>
        <nav className="title-entries" aria-label="入口">
          <a className="title-entry primary" href="/chat">
            <strong>チャット</strong>
            <span>キャラクタと話す</span>
          </a>
          <a className="title-entry" href="/designer">
            <strong>デザイナ</strong>
            <span>キャラクタを作る</span>
          </a>
        </nav>
        {message && <p role="alert" className="title-error">{message}</p>}
      </div>
    </main>
  );
}
