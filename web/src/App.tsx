// MyCompanion の Web の画面(本体仕様書 §15.1 の M7)。サインイン → 同意・年齢 → 会話・記憶・設定・アカウント。

import { useEffect, useMemo, useRef, useState } from "react";
import { remember, recall } from "./remember";
import { Account } from "./Account";
import { Api } from "./api";
import { createAuth, signInErrorMessage } from "./auth";
import { Chat } from "./Chat";
import { Consent } from "./Consent";
import { ErrorBoundary } from "./ErrorBoundary";
import { Memories } from "./Memories";
import { SettingsView } from "./SettingsView";
import { type AppConfig, loadConfig } from "./config";

type Phase = "loading" | "signed-out" | "consent" | "main" | "deleted" | "error";
type Tab = "chat" | "memories" | "settings" | "account";

const TABS: { id: Tab; label: string }[] = [
  { id: "chat", label: "会話" },
  { id: "memories", label: "記憶" },
  { id: "settings", label: "設定" },
  { id: "account", label: "アカウント" },
];

export function App() {
  const [config, configError] = useMemo((): [AppConfig | null, string] => {
    try {
      return [loadConfig(), ""];
    } catch (e) {
      return [null, (e as Error).message];
    }
  }, []);
  const auth = useMemo(() => (config ? createAuth(config) : null), [config]);
  const api = useMemo(() => (config && auth ? new Api(config.apiUrl, () => auth.token()) : null), [config, auth]);
  const [phase, setPhase] = useState<Phase>(configError ? "error" : "loading");
  // 見ていたページ(再表示のときに戻す。タブを閉じると消える)
  // 上部のメニューの高さを測り、ページの固定の行(キャラクタの選択)をそのすぐ下に置く(画面の幅で折り返すと高さが変わる)
  const headerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const header = headerRef.current;
    if (!header) return;
    const set = () => document.documentElement.style.setProperty("--app-header-height", `${header.offsetHeight}px`);
    set();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(set);
    observer.observe(header);
    return () => observer.disconnect();
  }, []);

  const [tab, setTabState] = useState<Tab>(() => {
    const saved = recall("tab");
    return TABS.some((t) => t.id === saved) ? (saved as Tab) : "chat";
  });
  const setTab = (next: Tab) => {
    setTabState(next);
    remember("tab", next);
  };
  // 記憶・設定・アカウントの画面は、会話の画面が出たあとに、裏で読み込んでおく(開いたときに待たせない)
  const [prefetch, setPrefetch] = useState(false);
  useEffect(() => {
    if (phase !== "main") return;
    const timer = window.setTimeout(() => setPrefetch(true), 1500);
    return () => window.clearTimeout(timer);
  }, [phase]);
  const [message, setMessage] = useState(configError);
  // サインインの画面へ移動中(ボタンを2回押させない。2回目は、MSAL が interaction_in_progress で断る)
  const [signingIn, setSigningIn] = useState(false);

  useEffect(() => {
    // サインインの画面から「戻る」で戻ったとき、ページが保存された状態のまま表示されることがある。押せる状態に戻す
    const reset = (event: PageTransitionEvent) => {
      if (event.persisted) setSigningIn(false);
    };
    window.addEventListener("pageshow", reset);
    return () => window.removeEventListener("pageshow", reset);
  }, []);

  async function afterSignIn() {
    const status = await api!.consentStatus();
    setPhase(status.missing.length === 0 ? "main" : "consent");
  }

  useEffect(() => {
    if (!auth || !api) return;
    (async () => {
      try {
        if (!(await auth.start())) {
          setPhase("signed-out");
          return;
        }
        await afterSignIn();
      } catch (e) {
        setMessage((e as Error).message);
        setPhase("error");
      }
    })();
    // afterSignIn は api だけに依存する
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth, api]);

  return (
    <div className="app">
      <header className="app-header" ref={headerRef}>
        <span className="app-name">MyCompanion</span>
        {phase === "main" && (
          <nav aria-label="画面" className="tabs">
            {TABS.map((t) => (
              <button key={t.id} className={tab === t.id ? "tab active" : "tab"} aria-current={tab === t.id ? "page" : undefined} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </nav>
        )}
        {(phase === "main" || phase === "consent") && auth && (
          <button className="link" onClick={() => auth.signOut().then(() => setPhase("signed-out"))}>
            サインアウト
          </button>
        )}
      </header>
      <main>
        <ErrorBoundary>
          {phase === "loading" && <p aria-busy="true">読み込んでいます…</p>}
          {phase === "error" && <p role="alert">{message}</p>}
          {phase === "signed-out" && auth && (
            <section className="signin">
              <h1>MyCompanion</h1>
              <p>AI のキャラクタが、あなたの気持ちを推しはかりながら会話するサービスです。キャラクタは AI であり、人間ではありません。</p>
              <button
                disabled={signingIn}
                onClick={async () => {
                  if (signingIn) return;
                  setSigningIn(true);
                  setMessage("");
                  try {
                    // 本物は、サインインの画面へ移る(戻ってきたら、起動時に結果を受け取る)。押せないままにしておく
                    if (await auth.signIn()) {
                      await afterSignIn();
                      setSigningIn(false);
                    }
                  } catch (e) {
                    setMessage(signInErrorMessage(e));
                    setSigningIn(false);
                  }
                }}
              >
                {signingIn ? "サインインの画面へ移動しています…" : "メールでサインイン"}
              </button>
              {message && <p role="alert">{message}</p>}
            </section>
          )}
          {phase === "consent" && api && <Consent api={api} onDone={() => setPhase("main")} />}
          {/* ページを切り替えても、画面を作り直さない(会話の続き・入力中の文字を残す)。見えないページは隠すだけ */}
          {phase === "main" && api && (
            <>
              <div hidden={tab !== "chat"}>
                <Chat api={api} />
              </div>
              {(prefetch || tab === "memories") && (
                <div hidden={tab !== "memories"}>
                  <Memories api={api} active={tab === "memories"} />
                </div>
              )}
              {(prefetch || tab === "settings") && (
                <div hidden={tab !== "settings"}>
                  <SettingsView api={api} active={tab === "settings"} />
                </div>
              )}
              {(prefetch || tab === "account") && (
                <div hidden={tab !== "account"}>
                  <Account api={api} active={tab === "account"} onDeleted={() => setPhase("deleted")} />
                </div>
              )}
            </>
          )}
          {phase === "deleted" && (
            <section className="signin" role="status">
              <h1>アカウントを削除しました</h1>
              <p>記憶・会話・関係・評価を削除しました。サインインの情報(メールアドレスなど)は、数時間以内に削除されます。ご利用ありがとうございました。</p>
            </section>
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
