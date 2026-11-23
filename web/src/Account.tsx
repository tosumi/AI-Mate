// アカウントの画面(本体仕様書 §13.3、§13.4.4、§13.4.5、§15.1 の M7)。
// データの書き出し(ダウンロード)、同意の確認と撤回、アカウントの削除(確認の入力と、完了の案内)。

import { useEffect, useState } from "react";
import type { Api, ConsentStatus } from "./api";
import { ConsentLabel } from "./consentLabel";
import { useRefreshOnOpen } from "./useRefreshOnOpen";

type Props = { api: Api; onDeleted: () => void; active?: boolean };

export function Account({ api, onDeleted, active = true }: Props) {
  const [status, setStatus] = useState<ConsentStatus | null>(null);
  const [message, setMessage] = useState("");
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.consentStatus().then(setStatus, (e: Error) => setMessage(e.message));
  }, [api]);

  // 開いたとき: 前の内容を出したまま、裏で新しくする
  useRefreshOnOpen(active, () => api.consentStatus().then(setStatus, () => undefined));

  async function download() {
    setMessage("");
    try {
      const data = await api.exportData();
      // ブラウザの保存領域には置かず、その場でファイルとして保存する
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `mycompanion-export-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      setMessage("書き出しました。");
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  async function revoke(kind: string, label: string) {
    const plain = label.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
    if (!window.confirm(`「${plain}」の同意を取り消しますか。`)) return;
    try {
      setStatus(await api.revokeConsent(kind));
      setMessage("取り消しました。");
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  async function remove() {
    setBusy(true);
    setMessage("");
    try {
      await api.deleteAccount();
      onDeleted();
    } catch (e) {
      setMessage((e as Error).message);
      setBusy(false);
    }
  }

  const items = status?.screens.flatMap((s) => s.items).filter((item) => item.granted) ?? [];
  return (
    <section className="panel" aria-labelledby="account-title">
      {/* 見出しは読み上げ用に残し、画面には出さない(2026-11-23 のユーザーの指示) */}
      <h1 id="account-title" className="visually-hidden">アカウント</h1>
      {message && <p role="status">{message}</p>}

      <h2>データの書き出し</h2>
      <p className="hint">設定、同意、関係、記憶、会話、感情の推定の記録、報告、評価を、JSON のファイルで保存します。</p>
      <button onClick={download}>書き出す</button>

      <h2>同意</h2>
      <p className="hint">同意書 版 {status?.version}。必須の同意を取り消すと、会話できなくなります。</p>
      <ul className="items">
        {items.map((item) => (
          <li key={item.type}>
            <div>
              <ConsentLabel text={item.label} />
              <span className="badge">{item.required ? "必須" : "任意"}</span>
            </div>
            <div className="actions">
              <button onClick={() => revoke(item.type, item.label)}>取り消す</button>
            </div>
          </li>
        ))}
      </ul>

      <h2>アカウントの削除</h2>
      <p className="hint">
        すべてのキャラクタとの記憶・会話・関係・評価と、サインインの情報(メールアドレスなど)を削除します。元に戻せません。サインインの情報は、数時間以内に削除されます。
      </p>
      <label>
        削除するには <code>delete</code> と入力してください{" "}
        <input value={confirmText} onChange={(e) => setConfirmText(e.target.value)} aria-label="確認の入力" />
      </label>
      <div className="choices">
        <button className="danger" disabled={busy || confirmText !== "delete"} onClick={remove}>
          アカウントを削除する
        </button>
      </div>
    </section>
  );
}
