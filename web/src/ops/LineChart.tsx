// 1系列の折れ線グラフ(外部のライブラリは使わない)。系列ごとに1つのグラフにする(縦軸を2つ持たない)。
// - 縦軸は 0 から。線は 2px、なぞると縦線と値を出す。表でも見られる(色だけに頼らない)。

import { useId, useMemo, useState } from "react";
import { formatTime, formatValue, niceMax } from "./labels";

const W = 360;
const H = 150;
const PAD = { left: 8, right: 8, top: 16, bottom: 26 };

type Props = { label: string; unit: string; points: [string, number][]; start: string; end: string };

export function LineChart({ label, unit, points, start, end }: Props) {
  const titleId = useId();
  const [hover, setHover] = useState<number | null>(null);
  const t0 = Date.parse(start);
  const t1 = Date.parse(end);
  const data = useMemo(() => points.map(([t, v]) => ({ t: Date.parse(t), v, iso: t })).filter((p) => p.t >= t0 && p.t <= t1), [points, t0, t1]);
  const max = niceMax(Math.max(0, ...data.map((p) => p.v)));
  const x = (t: number) => PAD.left + ((t - t0) / Math.max(1, t1 - t0)) * (W - PAD.left - PAD.right);
  const y = (v: number) => H - PAD.bottom - (v / max) * (H - PAD.top - PAD.bottom);
  const path = data.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const latest = data.length ? data[data.length - 1] : undefined;
  const active = hover === null ? null : data[hover];

  function onMove(event: React.PointerEvent<SVGSVGElement>) {
    if (data.length === 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * W;
    let best = 0;
    for (let i = 1; i < data.length; i += 1) if (Math.abs(x(data[i].t) - px) < Math.abs(x(data[best].t) - px)) best = i;
    setHover(best);
  }

  return (
    <figure className="ops-chart">
      <figcaption>
        <span id={titleId} className="ops-chart-label">{label}</span>
        <span className="ops-chart-latest">{active ? `${formatTime(active.iso)}  ${formatValue(active.v, unit)}` : latest ? `最新 ${formatValue(latest.v, unit)}` : "データなし"}</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={titleId} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {/* 目盛り: 0 と上端だけ(控えめに) */}
        <line className="ops-grid" x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} />
        <line className="ops-grid faint" x1={PAD.left} x2={W - PAD.right} y1={y(max)} y2={y(max)} />
        <text className="ops-axis" x={W - PAD.right} y={y(max) - 2} textAnchor="end">{formatValue(max, unit)}</text>
        <text className="ops-axis" x={PAD.left} y={H - 7}>{formatTime(start).slice(5, 16)}</text>
        <text className="ops-axis" x={W - PAD.right} y={H - 7} textAnchor="end">{formatTime(end).slice(5, 16)}</text>
        {data.length > 1 && <path className="ops-line" d={path} />}
        {data.length === 1 && <circle className="ops-dot" cx={x(data[0].t)} cy={y(data[0].v)} r={4} />}
        {active && (
          <>
            <line className="ops-crosshair" x1={x(active.t)} x2={x(active.t)} y1={PAD.top} y2={H - PAD.bottom} />
            <circle className="ops-dot" cx={x(active.t)} cy={y(active.v)} r={4} />
          </>
        )}
      </svg>
      <details className="ops-chart-table">
        <summary>表で見る</summary>
        <table>
          <thead>
            <tr><th scope="col">時刻</th><th scope="col">{label}</th></tr>
          </thead>
          <tbody>
            {data.map((p) => (
              <tr key={p.iso}><td>{formatTime(p.iso)}</td><td>{formatValue(p.v, unit)}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}
