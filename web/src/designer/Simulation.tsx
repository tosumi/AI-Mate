// 模擬のシミュレーション(標準シナリオ。LLM を呼ばない。ペルソナデザイナ仕様書 §9)。
// 一般ユーザーは模擬だけ(実際のモデルでの試しは、運営者の画面。2026-11-25 の決定)。

import { useState } from "react";
import type { Definition, DesignerApi, SimulationResult } from "./api";
import { Check } from "./fields";
import { MODE_LABELS, signed } from "./labels";

const START_OPTIONS: { value: number | null; label: string }[] = [
  { value: null, label: "初期値" }, { value: -10, label: "-10" }, { value: 0, label: "0" }, { value: 10, label: "+10" }, { value: 20, label: "+20" },
];

export function Simulation({ api, definition, canRun }: { api: DesignerApi; definition: Definition; canRun: boolean }) {
  const [starts, setStarts] = useState<(number | null)[]>([null]);
  const [under18, setUnder18] = useState(false);
  const [result, setResult] = useState<SimulationResult | null>(null);
  const [message, setMessage] = useState("");
  const [running, setRunning] = useState(false);

  async function run() {
    setRunning(true);
    setMessage("");
    try {
      setResult(await api.simulate(definition, starts, under18));
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className="dz-page" aria-labelledby="dz-sim-title">
      <h2 id="dz-sim-title">シミュレーション(模擬)</h2>
      <p className="dz-hint">標準のシナリオで、応答の種類(ふつう・短め・相づち・無言)の出方と、守るべき規則を確かめます。実際のモデルは呼ばず、文面は作りません。</p>
      <div className="dz-row">
        <span>開始時の関係値:</span>
        {START_OPTIONS.map((o) => (
          <Check key={String(o.value)} label={o.label} checked={starts.includes(o.value)}
            onChange={(on) => setStarts(on ? [...starts, o.value] : starts.filter((s) => s !== o.value))} />
        ))}
      </div>
      <Check label="18歳未満のユーザーとして試す" checked={under18} onChange={setUnder18} />
      <button className="dz-primary" disabled={!canRun || running || starts.length === 0} onClick={() => void run()}>
        {running ? "実行しています…" : "実行する"}
      </button>
      {!canRun && <p className="dz-hint">検査のエラーを直すと、実行できます。</p>}
      {message && <p role="alert" className="dz-error">{message}</p>}
      {result && (
        <div role="status">
          <p className={result.ok ? "dz-ok" : "dz-error"}>{result.ok ? "すべて合格" : "合格しなかったシナリオがあります"}</p>
          {result.runs.map((run) => (
            <div key={String(run.relationship)} className="dz-run">
              <h3>開始時の関係値: {run.relationship === null ? "キャラクタの初期値" : signed(run.relationship, 0)}</h3>
              <p>合格 {run.passed} / {run.total}</p>
              <ModeBars modes={run.modes} />
              <div className="dz-scroll">
                <table className="dz-table">
                  <thead><tr><th scope="col">シナリオ</th><th scope="col">結果</th><th scope="col">応答の種類</th><th scope="col">問題</th></tr></thead>
                  <tbody>
                    {run.scenarios.map((s) => (
                      <tr key={s.scenario}>
                        <td>{s.scenario} {s.title}</td>
                        <td className={s.ok ? "dz-ok" : "dz-error"}>{s.ok ? "合格" : "不合格"}</td>
                        <td>{Object.entries(s.modes).filter(([, n]) => n > 0).map(([m, n]) => `${MODE_LABELS[m] ?? m} ${n}`).join("・")}</td>
                        <td>{[...s.violations, ...s.failures].join(" / ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/** 応答の種類の割合(横の帯。色だけに頼らず、ラベルと数値を並べる)。 */
export function ModeBars({ modes }: { modes: Record<string, number> }) {
  const entries = Object.entries(modes);
  return (
    <div className="dz-bars">
      {entries.map(([mode, share]) => (
        <div key={mode} className="dz-bar-row">
          <span className="dz-bar-label">{MODE_LABELS[mode] ?? mode}</span>
          <span className="dz-bar-track"><span className="dz-bar-fill" data-width={Math.round(share * 100)} ref={(el) => { if (el) el.style.width = `${Math.round(share * 100)}%`; }} /></span>
          <span className="dz-bar-value">{Math.round(share * 100)}%</span>
        </div>
      ))}
    </div>
  );
}
