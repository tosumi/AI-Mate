// 評価の集計(設計者・承認者・サポート・管理者。本体仕様書 §15.1 の M9)。
// 件数と平均だけ。件数が少ない組は、サーバーが平均を出さない(少数の評価から、個人の点が推測されないように)。

import { useCallback, useEffect, useState } from "react";
import type { OpsApi, RatingRow } from "./api";
import { formatTime } from "./labels";

const RANGES = [
  { days: 7, label: "7日" },
  { days: 30, label: "30日" },
  { days: 90, label: "90日" },
];

export function Ratings({ api }: { api: OpsApi }) {
  const [days, setDays] = useState(30);
  const [minN, setMinN] = useState(5);
  const [rows, setRows] = useState<RatingRow[] | null>(null);
  const [since, setSince] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    setRows(null);
    try {
      const from = new Date(Date.now() - days * 24 * 3600 * 1000).toISOString();
      const result = await api.ratings(from, minN);
      setRows(result.rows);
      setSince(result.since);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [api, days, minN]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="ops-section" aria-labelledby="ops-ratings-title">
      <div className="ops-toolbar">
        <h2 id="ops-ratings-title">評価の集計</h2>
        <div className="ops-segmented" role="group" aria-label="期間">
          {RANGES.map((r) => (
            <button key={r.days} aria-pressed={days === r.days} onClick={() => setDays(r.days)}>{r.label}</button>
          ))}
        </div>
        <label className="ops-inline">
          平均を出す件数の下限
          <select value={minN} onChange={(e) => setMinN(Number(e.target.value))}>
            {[3, 5, 10, 20].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
      </div>
      {since && <p className="ops-muted">{formatTime(since)} 以降に更新された評価。件数が下限に満たない組は、平均を「—」で示します。</p>}
      {error && <p role="alert" className="ops-error">{error}</p>}
      {rows === null && !error && <p aria-busy="true">読み込んでいます…</p>}
      {rows?.length === 0 && <p className="ops-muted">この期間の評価はありません。</p>}
      {rows && rows.length > 0 && (
        <div className="ops-table-wrap">
          <table className="ops-table numeric">
            <thead>
              <tr>
                <th scope="col">キャラクタ(版)</th><th scope="col">作り方</th><th scope="col">モデル</th><th scope="col">プロンプト</th><th scope="col">応答の種類</th>
                <th scope="col">評価者</th><th scope="col">件数</th><th scope="col">総合</th><th scope="col">らしさ</th><th scope="col">自然さ</th><th scope="col">共感</th><th scope="col">低評価</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td>{r.character_id}{r.character_version != null && ` (v${r.character_version})`}</td>
                  <td>{r.compose_mode ?? "—"}</td><td>{r.reply_model ?? "—"}</td><td>{r.reply_prompt_version ?? "—"}</td><td>{r.response_mode ?? "—"}</td>
                  <td>{r.raters}</td><td>{r.n}</td>
                  <td>{fmt(r.overall)}</td><td>{fmt(r.persona_fit)}</td><td>{fmt(r.naturalness)}</td><td>{fmt(r.empathy)}</td><td>{r.low ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function fmt(value: number | null): string {
  return value == null ? "—" : value.toFixed(2);
}
