// 運営者の画面(/ops。本体仕様書 §15.1 の M9。2026-11-24 のユーザーの決定)。
// - 一般ユーザーの画面とは別の見た目。入口は URL だけ(一般ユーザーの画面からはリンクしない)。PC を主に、スマホでも読める。
// - ロールに応じてタブを出す(サーバーでも、操作ごとに権限を確かめる。画面の表示は補助)。
// - 30分操作がなければ、画面を消して、サインアウトする。

import { useEffect, useMemo, useState } from "react";
import { ErrorBoundary } from "../ErrorBoundary";
import { NewVersionBanner } from "../NewVersionBanner";
import { recall, remember } from "../remember";
import { ApiError } from "../api";
import { signInErrorMessage } from "../auth";
import { type Me, OpsApi } from "./api";
import { AuditLogs } from "./AuditLogs";
import { createOpsAuth } from "./auth";
import { loadOpsConfig } from "./config";
import { Dashboard } from "./Dashboard";
import { useIdleTimeout } from "./idle";
import { label, ROLE_LABELS } from "./labels";
import { Personas } from "./Personas";
import { Ratings } from "./Ratings";
import { Reports } from "./Reports";
import "./ops.css";

type Phase = "loading" | "unavailable" | "signed-out" | "main" | "timed-out" | "error";
type Tab = "reports" | "ratings" | "personas" | "audit" | "dashboard";

const TABS: { id: Tab; label: string; permission: string }[] = [
  { id: "reports", label: "報告", permission: "view_reports" },
  { id: "ratings", label: "評価", permission: "view_ratings" },
  { id: "personas", label: "ペルソナ", permission: "view_personas" },
  { id: "audit", label: "監査ログ", permission: "view_audit" },
  { id: "dashboard", label: "ダッシュボード", permission: "view_system" },
];
const IDLE_FLAG = "ops.idle-signed-out";

export function OpsApp() {
  const config = useMemo(() => loadOpsConfig(), []);
  const auth = useMemo(() => (config ? createOpsAuth(config) : null), [config]);
  const api = useMemo(() => (config && auth ? new OpsApi(config.apiUrl, () => auth.token()) : null), [config, auth]);
  const [phase, setPhase] = useState<Phase>(config ? "loading" : "unavailable");
  const [me, setMe] = useState<Me | null>(null);
  const [message, setMessage] = useState("");
  const [signingIn, setSigningIn] = useState(false);
  const [tab, setTabState] = useState<Tab | null>(() => recall("ops.tab") as Tab | null);

  useEffect(() => {
    document.title = "FureAI ペルソナ 運営";
  }, []);

  async function afterSignIn() {
    const profile = await api!.me();
    setMe(profile);
    setPhase("main");
  }

  useEffect(() => {
    if (!auth || !api) return;
    // 操作がなくてサインアウトしたあと(Entra から戻ってきた): その旨を出す
    if (recall(IDLE_FLAG)) {
      setMessage("30分操作がなかったため、サインアウトしました。");
      remember(IDLE_FLAG, "");
    }
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth, api]);

  // 30分操作がなければ: まず画面を消し(データを画面から外す)、そのあとサインアウトする
  useIdleTimeout(phase === "main", () => {
    setPhase("timed-out");
    setMe(null);
    remember(IDLE_FLAG, "1");
    auth?.signOut().catch(() => undefined);
  });

  const tabs = TABS.filter((t) => me?.permissions.includes(t.permission));
  const current = tabs.find((t) => t.id === tab)?.id ?? tabs[0]?.id ?? null;
  const setTab = (next: Tab) => {
    setTabState(next);
    remember("ops.tab", next);
  };

  async function signIn() {
    if (signingIn || !auth) return;
    setSigningIn(true);
    setMessage("");
    try {
      if (await auth.signIn()) {
        await afterSignIn();
        setSigningIn(false);
      }
    } catch (e) {
      // API の失敗(権限がないなど)は、その文面。サインインを始められなかったときは、決まった文面
      setMessage(e instanceof ApiError ? e.message : signInErrorMessage(e));
      setSigningIn(false);
      setPhase("signed-out");
    }
  }

  return (
    <div className="ops">
      <header className="ops-header">
        <span className="ops-brand">FureAI ペルソナ <span className="ops-badge">運営</span></span>
        {phase === "main" && tabs.length > 0 && (
          <nav aria-label="運営の画面" className="ops-tabs">
            {tabs.map((t) => (
              <button key={t.id} className={current === t.id ? "active" : undefined} aria-current={current === t.id ? "page" : undefined} onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </nav>
        )}
        {phase === "main" && me && (
          <div className="ops-user">
            <span className="ops-roles" aria-label="あなたのロール">{me.roles.map((r) => label(ROLE_LABELS, r)).join("・") || "ロールなし"}</span>
            <button className="ops-signout" onClick={() => auth?.signOut().then(() => setPhase("signed-out"))}>サインアウト</button>
          </div>
        )}
      </header>
      <NewVersionBanner className="ops-update-banner" />
      <main className="ops-main">
        <ErrorBoundary>
          {phase === "loading" && <p aria-busy="true">読み込んでいます…</p>}
          {phase === "unavailable" && <p role="alert">この環境では、運営者の画面は使えません。</p>}
          {phase === "error" && <p role="alert" className="ops-error">{message}</p>}
          {(phase === "signed-out" || phase === "timed-out") && (
            <section className="ops-signin">
              <h1>運営者のサインイン</h1>
              {phase === "timed-out" && <p role="status">30分操作がなかったため、画面を閉じてサインアウトしました。</p>}
              <p className="ops-muted">運営者のアカウント(ロールを割り当てた組織のアカウント)でサインインします。操作は、すべて監査ログに記録されます。</p>
              <button className="primary" disabled={signingIn} onClick={() => void signIn()}>
                {signingIn ? "サインインの画面へ移動しています…" : "サインイン"}
              </button>
              {message && <p role="alert">{message}</p>}
            </section>
          )}
          {phase === "main" && api && me && (
            <>
              {tabs.length === 0 && <p className="ops-muted">あなたのロールで見られる画面はありません。</p>}
              {current === "reports" && <Reports api={api} canHandle={me.permissions.includes("handle_reports")} />}
              {current === "ratings" && <Ratings api={api} />}
              {current === "personas" && <Personas api={api} permissions={me.permissions} />}
              {current === "audit" && <AuditLogs api={api} />}
              {current === "dashboard" && <Dashboard api={api} />}
            </>
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
