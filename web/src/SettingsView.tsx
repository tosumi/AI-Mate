// 設定の画面(本体仕様書 §10.3、§12、§15.1 の M7・M8)。無言、無言の表示、話題の連想、キャラクタから話しかけること。
// 応答の作り方・フィルタ(検証用)と、年齢(サインアップの手順で決める)は、ここでは変えない。

import { useEffect, useState } from "react";
import type { Api, Settings } from "./api";
import { type PushState, disablePush, enablePush, pushState } from "./push";
import { useRefreshOnOpen } from "./useRefreshOnOpen";

// 「現在の設定：」のあとに、太字で出す状態(2026-11-21 のユーザーの指示)
const PUSH_TEXT: Record<PushState, string> = {
  unsupported: "このブラウザでは通知を使えません",
  disabled: "通知はいまは使えません",
  denied: "このブラウザで通知が拒否されています",
  off: "この端末では通知を受け取りません",
  on: "この端末で通知を受け取ります",
};
// ブラウザの許可が得られなかったときの案内(確認が出ない・閉じた・ブロックされた)
const PERMISSION_HINT =
  "ブラウザの通知の許可が得られませんでした。アドレスバーの通知のアイコン、またはサイトのアクセス許可から「許可」にしてください。"

export function SettingsView({ api, active = true }: { api: Api; active?: boolean }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [message, setMessage] = useState("");
  const [push, setPush] = useState<PushState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);

  useEffect(() => {
    api.settings().then(setSettings, (e: Error) => setMessage(e.message));
    pushState(api).then(setPush, () => setPush("disabled"));
  }, [api]);

  // 開いたとき: 前の内容を出したまま、裏で新しくする(ほかの端末で変えた設定など)
  useRefreshOnOpen(active, () => api.settings().then(setSettings, () => undefined));

  async function togglePush() {
    // 押したあとに何が起きたかを、必ず表示する(ブラウザが許可の確認を出さないことがある)
    setMessage("");
    setPushBusy(true);
    try {
      const wanted = push === "on" ? "off" : "on";
      const next = wanted === "off" ? await disablePush(api) : await enablePush(api);
      setPush(next);
      if (wanted === "on" && next !== "on") setMessage(PERMISSION_HINT);
    } catch (e) {
      setMessage(`通知の設定に失敗しました: ${(e as Error).message}`);
    } finally {
      setPushBusy(false);
    }
  }

  async function update(changes: Partial<Settings>) {
    // 押したらすぐ表示を変え、保存に失敗したら元に戻す
    const before = settings;
    setSettings((current) => (current ? { ...current, ...changes } : current));
    setMessage("");
    try {
      setSettings(await api.updateSettings(changes));
      setMessage("保存しました。");
    } catch (e) {
      setSettings(before);
      setMessage((e as Error).message);
    }
  }

  if (!settings) return <p aria-busy="true">{message || "読み込んでいます…"}</p>;
  return (
    <section className="panel" aria-labelledby="settings-title">
      {/* 見出しは読み上げ用に残し、画面には出さない(2026-11-23 のユーザーの指示) */}
      <h1 id="settings-title" className="visually-hidden">設定</h1>
      <fieldset>
        <legend>キャラクタが黙ること</legend>
        <label>
          <input
            type="checkbox"
            checked={settings.silence_enabled}
            onChange={(e) => update({ silence_enabled: e.target.checked })}
          />{" "}
          キャラクタが、黙ったり、相づちだけで返したりしてよい
        </label>
        <p className="hint">性格によっては、無理に話さないことがあります。危険を感じる話や、怒っているときなどは、黙りません。</p>
        <label>
          黙ったときの表示{" "}
          <select value={settings.silence_render} onChange={(e) => update({ silence_render: e.target.value })}>
            <option value="dots">「…」を表示する</option>
            <option value="none">何も表示しない</option>
          </select>
        </label>
      </fieldset>
      <fieldset>
        <legend>話題</legend>
        <label>
          <input
            type="checkbox"
            checked={settings.association_enabled}
            onChange={(e) => update({ association_enabled: e.target.checked })}
          />{" "}
          話が途切れそうなとき、キャラクタが、つながりのある話題へ自分から話を移してよい
        </label>
      </fieldset>
      <fieldset>
        <legend>キャラクタから話しかけること</legend>
        <label>
          <input
            type="checkbox"
            checked={settings.proactive_enabled}
            onChange={(e) => update({ proactive_enabled: e.target.checked })}
          />{" "}
          しばらく会話がないとき、キャラクタから話しかけてよい
        </label>
        <p className="hint">
          話題があるときだけ、短く話しかけます。夜21時から朝9時までは話しかけません。返事がないあいだは、続けて話しかけません。
          回数は性格によって、1日1〜3回までです。話しかけたメッセージは、次に会話の画面を開いたときに表示します。
        </p>
        {push && (
          <div className="push">
            <p className="push-status" role="status">
              現在の設定：<strong>{PUSH_TEXT[push]}</strong>
            </p>
            {push === "denied" && <p className="hint">{PERMISSION_HINT}</p>}
            {(push === "on" || push === "off") && (
              <button type="button" className="raised" onClick={togglePush} disabled={pushBusy}>
                {pushBusy ? "準備しています…" : push === "on" ? "この端末で通知を受け取らない" : "この端末で通知を受け取る"}
              </button>
            )}
            <p className="hint">通知には、会話の内容は表示しません(「ハルからメッセージがあります」のように、キャラクタの名前だけ)。</p>
          </div>
        )}
      </fieldset>
      {message && <p role="status">{message}</p>}
    </section>
  );
}
