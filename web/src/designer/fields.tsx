// デザイナの入力の部品(数値のスライダー、文字、選択)。スマホでも触れる大きさにする。
// path(定義の場所)を付けると、検査の結果から、その欄へ移れる(data-path)。

import { useId } from "react";

export function Slider({ label, value, min, max, step = 0.05, onChange, hint, format, path }: {
  label: string; value: number; min: number; max: number; step?: number; onChange: (v: number) => void;
  hint?: string; format?: (v: number) => string; path?: string;
}) {
  const id = useId();
  return (
    <div className="dz-field dz-slider" data-path={path}>
      <label htmlFor={id}>{label}<YamlKey path={path} /></label>
      <input id={id} type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <input
        type="number" aria-label={`${label}(数値)`} min={min} max={max} step={step} value={value}
        onChange={(e) => e.target.value !== "" && onChange(Number(e.target.value))}
      />
      {format && <span className="dz-value">{format(value)}</span>}
      {hint && <span className="dz-hint">{hint}</span>}
    </div>
  );
}

export function TextField({ label, value, onChange, multiline = false, maxLength, placeholder, hint, path }: {
  label: string; value: string; onChange: (v: string) => void; multiline?: boolean; maxLength?: number; placeholder?: string; hint?: string;
  path?: string;
}) {
  const id = useId();
  return (
    <div className="dz-field dz-text" data-path={path}>
      <label htmlFor={id}>{label}<YamlKey path={path} /></label>
      {multiline ? (
        <textarea id={id} value={value} maxLength={maxLength} rows={4} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input id={id} type="text" value={value} maxLength={maxLength} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      )}
      {(hint || maxLength) && (
        <span className="dz-hint">{hint}{maxLength ? `(${value.length} / ${maxLength})` : ""}</span>
      )}
    </div>
  );
}

export function Select({ label, value, options, onChange, hint, path }: {
  label: string; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void; hint?: string;
  path?: string;
}) {
  const id = useId();
  return (
    <div className="dz-field" data-path={path}>
      <label htmlFor={id}>{label}<YamlKey path={path} /></label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
      {hint && <span className="dz-hint">{hint}</span>}
    </div>
  );
}

export function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="dz-check">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

/** 欄の、YAML での名前(定義の場所)。画面の名前と YAML を見比べられるように併記する。
 *  読み上げと欄の名前には含めない(画面の名前だけで操作できるように)。 */
export function YamlKey({ path }: { path?: string }) {
  return path ? <code className="dz-key" aria-hidden="true">{path}</code> : null;
}
