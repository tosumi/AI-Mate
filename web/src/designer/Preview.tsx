// プレビュー: レーダーチャート、派生指標、lint の結果(ペルソナデザイナ仕様書 §6、§8)。
// 編集のたびに、サーバーの検査(CLI と同じ処理)の結果で描き直す。

import type { CheckResult, Resolved } from "./api";
import { ASSOCIATION_LABELS, LENGTH_LABELS, signed, TRAIT_KEYS, TRAIT_LABELS } from "./labels";
import { sectionForPath } from "./sectionList";

/** onJump があれば、場所のある検査の結果を押すと、原因の設定の項目へ移る(デザイナ)。 */
export function Preview({ check, busy, error, onJump }: {
  check: CheckResult | null; busy: boolean; error: string; onJump?: (path: string) => void;
}) {
  const resolved = check?.resolved ?? null;
  const errors = check?.findings.filter((f) => f.level === "error") ?? [];
  const warnings = check?.findings.filter((f) => f.level === "warning") ?? [];
  const infos = check?.findings.filter((f) => f.level === "info") ?? [];
  return (
    <aside className="dz-preview" aria-label="プレビュー" aria-busy={busy}>
      {error && <p role="alert" className="dz-error">{error}</p>}
      {!check && !error && <p aria-busy="true">計算しています…</p>}
      {check && (
        <>
          <section aria-labelledby="dz-lint-title">
            <h3 id="dz-lint-title">検査(lint)</h3>
            <p className={errors.length ? "dz-error" : "dz-ok"} role="status">
              {errors.length ? `エラー ${errors.length}件` : "エラーなし"}・警告 {warnings.length}件・情報 {infos.length}件
            </p>
            {check.uses_sample_id && <p className="dz-warning">サンプルの ID のままです。保存するときは、自分の ID に変えてください。</p>}
            {[...errors, ...warnings, ...infos].length > 0 && (
              <ul className="dz-findings">
                {[...errors, ...warnings, ...infos].map((f, i) => (
                  <li key={i} className={`dz-finding ${f.level}`}>
                    {onJump && f.path && sectionForPath(f.path) ? (
                      <button className="dz-jump" onClick={() => onJump(f.path)} title="原因の設定へ移る">
                        <span className="dz-code">{f.code}</span> {f.message}
                        <span className="dz-path"> ({f.path})</span>
                      </button>
                    ) : (
                      <>
                        <span className="dz-code">{f.code}</span> {f.message}
                        {f.path && <span className="dz-path"> ({f.path})</span>}
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
          {resolved ? <Derived resolved={resolved} /> : <p className="dz-hint">定義の形が正しくないため、解決済みの値を計算できません。</p>}
        </>
      )}
    </aside>
  );
}

function Derived({ resolved }: { resolved: Resolved }) {
  return (
    <>
      <section aria-labelledby="dz-radar-title">
        <h3 id="dz-radar-title">表出のパラメータ{resolved.type_id ? `(${resolved.type_id})` : ""}</h3>
        <Radar values={resolved.traits} defaults={resolved.defaults} />
        <table className="dz-table">
          <thead><tr><th scope="col">項目</th><th scope="col">いまの値</th><th scope="col">タイプの既定値</th></tr></thead>
          <tbody>
            {TRAIT_KEYS.map((k) => (
              <tr key={k}><td>{TRAIT_LABELS[k]}</td><td>{resolved.traits[k]?.toFixed(2)}</td><td>{resolved.defaults[k]?.toFixed(2)}</td></tr>
            ))}
            {Object.entries(resolved.association).map(([k, v]) => (
              <tr key={k}><td>{ASSOCIATION_LABELS[k] ?? k}</td><td>{v.toFixed(2)}</td><td>—</td></tr>
            ))}
          </tbody>
        </table>
        <p className="dz-card">{resolved.style_card}</p>
      </section>
      <section aria-labelledby="dz-curve-title">
        <h3 id="dz-curve-title">関係値ごとの変化</h3>
        <div className="dz-scroll">
          <table className="dz-table">
            <thead>
              <tr><th scope="col">関係値</th><th scope="col">口数</th><th scope="col">応答の長さ</th><th scope="col">表出</th><th scope="col">表出の閾値 / 上限</th><th scope="col">同期</th></tr>
            </thead>
            <tbody>
              {resolved.curve.map((p) => (
                <tr key={p.relationship}>
                  <td>{signed(p.relationship, 0)}</td>
                  <td>{p.talkativeness_eff.toFixed(2)}</td>
                  <td>{LENGTH_LABELS[p.length] ?? p.length}</td>
                  <td>{p.expressiveness_eff.toFixed(2)}</td>
                  <td>{p.expression_threshold.toFixed(2)} / {p.expression_cap.toFixed(2)}</td>
                  <td>{(resolved.sync_curve.find((s) => s.relationship === p.relationship)?.weight ?? 0).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="dz-hint">
          感情の半減期: {resolved.half_life_turns === null ? "—" : `約 ${resolved.half_life_turns} ターン`}(感情を引きずる長さの目安)
        </p>
      </section>
      {resolved.preferences.length > 0 && (
        <section aria-labelledby="dz-pref-title">
          <h3 id="dz-pref-title">嗜好</h3>
          <ul className="dz-list">
            {resolved.preferences.map((p) => (
              <li key={`${p.source}-${p.topic}`}>
                {p.topic} {signed(p.affinity)}
                <span className="dz-hint">{{ explicit: "", sheet: "(好き嫌いシート)" }[p.source] ?? "(話題ルールから導出)"}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

const SIZE = 260;
const CENTER = SIZE / 2;
const RADIUS = 92;

/** 6つの表出パラメータ(0〜1)のレーダーチャート。いまの値と、性格タイプの既定値を重ねる。 */
export function Radar({ values, defaults, compare, labels = ["いまの値", "タイプの既定値"] }: {
  values: Record<string, number>; defaults?: Record<string, number>; compare?: Record<string, number>; labels?: [string, string];
}) {
  const other = compare ?? defaults;
  const point = (i: number, v: number) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / TRAIT_KEYS.length;
    return [CENTER + Math.cos(angle) * RADIUS * v, CENTER + Math.sin(angle) * RADIUS * v] as const;
  };
  const polygon = (data: Record<string, number>) => TRAIT_KEYS.map((k, i) => point(i, Math.max(0, Math.min(1, data[k] ?? 0))).join(",")).join(" ");
  return (
    <figure className="dz-radar">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="表出のパラメータのレーダーチャート(数値は下の表)">
        {[0.25, 0.5, 0.75, 1].map((r) => (
          <polygon key={r} className="dz-radar-grid" points={TRAIT_KEYS.map((_, i) => point(i, r).join(",")).join(" ")} />
        ))}
        {TRAIT_KEYS.map((k, i) => {
          const [x, y] = point(i, 1);
          const [lx, ly] = point(i, 1.2);
          return (
            <g key={k}>
              <line className="dz-radar-grid" x1={CENTER} y1={CENTER} x2={x} y2={y} />
              <text className="dz-radar-label" x={lx} y={ly} textAnchor="middle" dominantBaseline="middle">{TRAIT_LABELS[k]}</text>
            </g>
          );
        })}
        {other && <polygon className="dz-radar-other" points={polygon(other)} />}
        <polygon className="dz-radar-main" points={polygon(values)} />
        {TRAIT_KEYS.map((k, i) => {
          const [x, y] = point(i, Math.max(0, Math.min(1, values[k] ?? 0)));
          return (
            <circle key={k} className="dz-radar-dot" cx={x} cy={y} r={4}>
              <title>{`${TRAIT_LABELS[k]} ${(values[k] ?? 0).toFixed(2)}`}</title>
            </circle>
          );
        })}
      </svg>
      {other && (
        <figcaption className="dz-legend">
          <span className="dz-swatch main" aria-hidden="true" />{labels[0]}
          <span className="dz-swatch other" aria-hidden="true" />{labels[1]}
        </figcaption>
      )}
    </figure>
  );
}
