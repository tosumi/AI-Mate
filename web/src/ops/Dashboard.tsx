// ダッシュボード: Azure の状況(管理者だけ。本体仕様書 §15.1 の M9)。数値だけで、ユーザーのデータは扱わない。
// サーバーは5分間キャッシュするので、読み直しても、すぐには変わらない。

import { useCallback, useEffect, useState } from "react";
import type { OpsApi, Period, SystemMetrics } from "./api";
import { formatTime } from "./labels";
import { LineChart } from "./LineChart";

const PERIODS: { id: Period; label: string }[] = [
  { id: "24h", label: "24時間" },
  { id: "7d", label: "7日" },
  { id: "30d", label: "30日" },
];

export function Dashboard({ api }: { api: OpsApi }) {
  const [period, setPeriod] = useState<Period>("24h");
  const [data, setData] = useState<SystemMetrics | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p: Period) => {
    setLoading(true);
    setError("");
    try {
      setData(await api.systemMetrics(p));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void load(period);
  }, [load, period]);

  return (
    <section className="ops-section" aria-labelledby="ops-dashboard-title">
      <div className="ops-toolbar">
        <h2 id="ops-dashboard-title">ダッシュボード</h2>
        <div className="ops-segmented" role="group" aria-label="期間">
          {PERIODS.map((p) => (
            <button key={p.id} aria-pressed={period === p.id} onClick={() => setPeriod(p.id)}>{p.label}</button>
          ))}
        </div>
        <button onClick={() => void load(period)} disabled={loading}>読み直す</button>
        {data && <span className="ops-muted">集計: {formatTime(data.generated_at)}(5分ごとに更新)</span>}
      </div>
      {loading && !data && <p aria-busy="true">読み込んでいます…</p>}
      {error && <p role="alert" className="ops-error">{error}</p>}
      {data && (
        <div className="ops-panels">
          {data.panels.map((panel) => (
            <article key={panel.id} className="ops-panel" aria-labelledby={`ops-panel-${panel.id}`}>
              <h3 id={`ops-panel-${panel.id}`}>{panel.title}</h3>
              {!panel.configured && <p className="ops-muted">この環境では、対象のリソースを設定していません。</p>}
              {panel.configured && !panel.available && <p className="ops-error">いまは読めませんでした。少し待って、読み直してください。</p>}
              {panel.available && (
                <div className="ops-charts">
                  {panel.series.map((s) => (
                    <LineChart key={s.id} label={s.label} unit={s.unit} points={s.points} start={data.start} end={data.end} />
                  ))}
                </div>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
