// 性格タイプの一覧と、全数確認(16タイプ × 表出プロファイル。模擬。ペルソナデザイナ仕様書 §7.1、§9.6)。

import { useState } from "react";
import type { Choices, DesignerApi, SweepRow } from "./api";
import { MODE_LABELS, TRAIT_LABELS } from "./labels";

export function Types({ api, choices }: { api: DesignerApi; choices: Choices }) {
  const [rows, setRows] = useState<SweepRow[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const profileName = (id: string | null) => (id ? choices.profiles.find((p) => p.id === id)?.name ?? id : "なし");

  async function loadSweep() {
    setLoading(true);
    setMessage("");
    try {
      setRows((await api.sweep()).rows);
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="dz-page" aria-labelledby="dz-types-title">
      <h2 id="dz-types-title">性格タイプの一覧</h2>
      <div className="dz-scroll">
        <table className="dz-table">
          <thead>
            <tr><th scope="col">タイプ</th><th scope="col">構成</th><th scope="col">{TRAIT_LABELS.talkativeness}</th><th scope="col">{TRAIT_LABELS.expressiveness}</th><th scope="col">{TRAIT_LABELS.initiative}</th><th scope="col">文体</th></tr>
          </thead>
          <tbody>
            {choices.types.map((t) => (
              <tr key={t.type_id}>
                <td>{t.type_id} {t.name}</td><td>{t.composition}</td>
                <td>{t.defaults.talkativeness?.toFixed(2)}</td><td>{t.defaults.expressiveness?.toFixed(2)}</td><td>{t.defaults.initiative?.toFixed(2)}</td>
                <td className="dz-wrap">{t.style_card}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>全数確認(16タイプ × 表出プロファイル)</h2>
      <p className="dz-hint">80通りを、標準のシナリオで模擬して、応答の種類の出方と、規則の違反がないかを見ます(最初の1回は 20 秒ほどかかります)。</p>
      <button onClick={() => void loadSweep()} disabled={loading}>{loading ? "計算しています…" : "全数確認を表示"}</button>
      {message && <p role="alert" className="dz-error">{message}</p>}
      {rows && (
        <div className="dz-scroll">
          <table className="dz-table">
            <thead>
              <tr><th scope="col">タイプ</th><th scope="col">プロファイル</th>{["full", "brief", "backchannel", "silence"].map((m) => <th key={m} scope="col">{MODE_LABELS[m]}</th>)}<th scope="col">違反・不合格</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.type_id}-${r.profile}`}>
                  <td>{r.type_id}</td><td>{profileName(r.profile)}</td>
                  {["full", "brief", "backchannel", "silence"].map((m) => <td key={m}>{Math.round((r.modes[m] ?? 0) * 100)}%</td>)}
                  <td className={r.violations.length + r.failures.length ? "dz-error" : undefined}>{r.violations.length + r.failures.length || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
