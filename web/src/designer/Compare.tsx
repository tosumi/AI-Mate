// 並べて比較(2026-11-25 の決定): 編集中とサンプル、編集中と別の YAML、編集前と編集後、性格タイプどうし。

import { useEffect, useState } from "react";
import type { CheckResult, Choices, Definition, DesignerApi } from "./api";
import { diffDefinitions } from "./draft";
import { Select } from "./fields";
import { LENGTH_LABELS, TRAIT_KEYS, TRAIT_LABELS } from "./labels";
import { Radar } from "./Preview";

type Target = "sample" | "yaml" | "baseline" | "types";

export function Compare({ api, choices, definition, baseline, current }: {
  api: DesignerApi; choices: Choices; definition: Definition; baseline: Definition; current: CheckResult | null;
}) {
  const [target, setTarget] = useState<Target>("baseline");
  const [samples, setSamples] = useState<{ character_id: string; name: string }[]>([]);
  const [sampleId, setSampleId] = useState("");
  const [other, setOther] = useState<{ definition: Definition; check: CheckResult; label: string } | null>(null);
  const [typeA, setTypeA] = useState(choices.types[0]?.type_id ?? "");
  const [typeB, setTypeB] = useState(choices.types[1]?.type_id ?? "");
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.samples().then((r) => setSamples(r.samples), () => undefined);
  }, [api]);

  async function load(def: Definition, label: string) {
    setMessage("");
    try {
      setOther({ definition: def, check: await api.check(def), label });
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  useEffect(() => {
    setOther(null);
    if (target === "baseline") void load(baseline, "編集前");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, baseline]);

  const diff = other && target !== "types" ? diffDefinitions(other.definition, definition) : [];
  return (
    <section className="dz-page" aria-labelledby="dz-compare-title">
      <h2 id="dz-compare-title">並べて比較</h2>
      <div className="dz-segmented" role="group" aria-label="比べる相手">
        {([["baseline", "編集前と編集後"], ["sample", "サンプルと"], ["yaml", "別の YAML と"], ["types", "性格タイプどうし"]] as [Target, string][]).map(([id, label]) => (
          <button key={id} aria-pressed={target === id} onClick={() => setTarget(id)}>{label}</button>
        ))}
      </div>
      {target === "sample" && (
        <Select label="サンプル" value={sampleId}
          options={[{ value: "", label: "(選んでください)" }, ...samples.map((s) => ({ value: s.character_id, label: s.name }))]}
          onChange={async (v) => {
            setSampleId(v);
            if (v) {
              try {
                const s = await api.sample(v);
                void load(s.definition, `サンプル: ${String(s.definition.name ?? v)}`);
              } catch (e) {
                setMessage((e as Error).message);
              }
            }
          }} />
      )}
      {target === "yaml" && (
        <label className="dz-field">
          比べる YAML のファイル
          <input type="file" accept=".yaml,.yml,text/yaml" onChange={async (e) => {
            const file = e.target.files?.[0];
            if (!file) return;
            try {
              const parsed = await api.parseYaml(await file.text());
              void load(parsed.definition, file.name);
            } catch (err) {
              setMessage((err as Error).message);
            }
          }} />
        </label>
      )}
      {message && <p role="alert" className="dz-error">{message}</p>}
      {target === "types" ? (
        <TypeCompare choices={choices} a={typeA} b={typeB} setA={setTypeA} setB={setTypeB} />
      ) : (
        other && current?.resolved && (
          <>
            <SideBySide left={{ label: "編集中", check: current }} right={{ label: other.label, check: other.check }} />
            <h3>変わった項目({diff.length})</h3>
            {diff.length === 0 ? <p className="dz-hint">違いはありません。</p> : (
              <div className="dz-scroll">
                <table className="dz-table dz-diff">
                  <thead><tr><th scope="col">項目</th><th scope="col">{other.label}</th><th scope="col">編集中</th></tr></thead>
                  <tbody>
                    {diff.slice(0, 200).map((d) => (
                      <tr key={d.path}><td><code>{d.path}</code></td><td>{show(d.before)}</td><td>{show(d.after)}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )
      )}
    </section>
  );
}

function show(value: unknown): string {
  if (value === undefined) return "(なし)";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 120 ? `${text.slice(0, 120)}…` : text;
}

function SideBySide({ left, right }: { left: { label: string; check: CheckResult }; right: { label: string; check: CheckResult } }) {
  const a = left.check.resolved;
  const b = right.check.resolved;
  if (!a || !b) return <p className="dz-hint">どちらかの定義の形が正しくないため、比べられません。</p>;
  return (
    <>
      <Radar values={a.traits} compare={b.traits} labels={[left.label, right.label]} />
      <div className="dz-scroll">
        <table className="dz-table">
          <thead><tr><th scope="col">項目</th><th scope="col">{left.label}</th><th scope="col">{right.label}</th></tr></thead>
          <tbody>
            <tr><td>性格タイプ</td><td>{a.type_id ?? "—"}</td><td>{b.type_id ?? "—"}</td></tr>
            {TRAIT_KEYS.map((k) => (
              <tr key={k}><td>{TRAIT_LABELS[k]}</td><td>{a.traits[k]?.toFixed(2)}</td><td>{b.traits[k]?.toFixed(2)}</td></tr>
            ))}
            <tr><td>応答の長さ(関係値 0)</td><td>{lengthAt(a.curve)}</td><td>{lengthAt(b.curve)}</td></tr>
            <tr><td>感情の半減期</td><td>{a.half_life_turns ?? "—"}</td><td>{b.half_life_turns ?? "—"}</td></tr>
            <tr><td>検査のエラー・警告</td><td>{counts(left.check)}</td><td>{counts(right.check)}</td></tr>
          </tbody>
        </table>
      </div>
      <div className="dz-cards">
        <p className="dz-card"><strong>{left.label}:</strong> {a.style_card}</p>
        <p className="dz-card"><strong>{right.label}:</strong> {b.style_card}</p>
      </div>
    </>
  );
}

function lengthAt(curve: { relationship: number; length: string }[]): string {
  const p = curve.find((c) => c.relationship === 0);
  return p ? LENGTH_LABELS[p.length] ?? p.length : "—";
}

function counts(check: CheckResult): string {
  const e = check.findings.filter((f) => f.level === "error").length;
  const w = check.findings.filter((f) => f.level === "warning").length;
  return `エラー ${e}・警告 ${w}`;
}

function TypeCompare({ choices, a, b, setA, setB }: { choices: Choices; a: string; b: string; setA: (v: string) => void; setB: (v: string) => void }) {
  const ta = choices.types.find((t) => t.type_id === a);
  const tb = choices.types.find((t) => t.type_id === b);
  const options = choices.types.map((t) => ({ value: t.type_id, label: `${t.type_id} ${t.name}` }));
  return (
    <>
      <div className="dz-row">
        <Select label="タイプ1" value={a} options={options} onChange={setA} />
        <Select label="タイプ2" value={b} options={options} onChange={setB} />
      </div>
      {ta && tb && (
        <>
          <Radar values={ta.defaults} compare={tb.defaults} labels={[`${ta.type_id} ${ta.name}`, `${tb.type_id} ${tb.name}`]} />
          <div className="dz-scroll">
            <table className="dz-table">
              <thead><tr><th scope="col">項目</th><th scope="col">{ta.type_id} {ta.name}</th><th scope="col">{tb.type_id} {tb.name}</th></tr></thead>
              <tbody>
                <tr><td>構成</td><td>{ta.composition}</td><td>{tb.composition}</td></tr>
                {TRAIT_KEYS.map((k) => (
                  <tr key={k}><td>{TRAIT_LABELS[k]}(既定値)</td><td>{ta.defaults[k]?.toFixed(2)}</td><td>{tb.defaults[k]?.toFixed(2)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="dz-cards">
            <p className="dz-card"><strong>{ta.type_id}:</strong> {ta.style_card}</p>
            <p className="dz-card"><strong>{tb.type_id}:</strong> {tb.style_card}</p>
          </div>
        </>
      )}
    </>
  );
}
