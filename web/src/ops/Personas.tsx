// キャラクタの設計・公開(運営者の画面。本体仕様書 §15.1 の M10 の手順4)。
// - 版の一覧: 公開中・非推奨の版、版どうしの差分、ロールバック・非推奨(承認者)。
// - 提出・試す: YAML を開いて検査し、提出する(設計者)。実際のモデルで話してみる・シミュレーションする(DB に書かない)。
// - レビュー: 承認待ちの提出を確かめ、承認して公開する・差し戻す(承認者。提出した本人は承認できない)。

import { useCallback, useEffect, useState } from "react";
import type { Inspection, OpsApi, PersonaCharacter, PersonaSubmission, PreviewAction, SubmissionDetail } from "./api";
import { formatTime } from "./labels";

type View = "versions" | "submit" | "review";
const STATUS: Record<string, string> = { published: "公開中", deprecated: "非推奨", draft: "下書き", review: "レビュー中", pending: "承認待ち", approved: "承認済み", rejected: "差し戻し" };

export function Personas({ api, permissions }: { api: OpsApi; permissions: string[] }) {
  const canSubmit = permissions.includes("submit_personas");
  const canPublish = permissions.includes("publish_personas");
  const canPreview = permissions.includes("preview_personas");
  const views: { id: View; label: string; show: boolean }[] = [
    { id: "versions", label: "版の一覧", show: true },
    { id: "submit", label: canSubmit ? "提出・試す" : "試す", show: canSubmit || canPreview },
    { id: "review", label: "レビュー", show: true },
  ];
  const [view, setView] = useState<View>(canPublish ? "review" : "versions");
  return (
    <section className="ops-section" aria-labelledby="ops-personas-title">
      <div className="ops-toolbar">
        <h2 id="ops-personas-title">ペルソナ</h2>
        <div className="ops-segmented" role="group" aria-label="ペルソナの画面">
          {views.filter((v) => v.show).map((v) => (
            <button key={v.id} aria-pressed={view === v.id} onClick={() => setView(v.id)}>{v.label}</button>
          ))}
        </div>
      </div>
      {view === "versions" && <Versions api={api} canPublish={canPublish} />}
      {view === "submit" && <SubmitAndPreview api={api} canSubmit={canSubmit} canPreview={canPreview} />}
      {view === "review" && <Review api={api} canPublish={canPublish} />}
    </section>
  );
}

// --- 版の一覧 ------------------------------------------------------------------------------------

function Versions({ api, canPublish }: { api: OpsApi; canPublish: boolean }) {
  const [characters, setCharacters] = useState<PersonaCharacter[] | null>(null);
  const [message, setMessage] = useState("");
  const [diff, setDiff] = useState<{ title: string; changes: { path: string; before: unknown; after: unknown }[] } | null>(null);
  const load = useCallback(async () => {
    try {
      setCharacters((await api.personas()).characters);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }, [api]);
  useEffect(() => {
    void load();
  }, [load]);

  async function act(run: () => Promise<string>) {
    setMessage("");
    try {
      setMessage(await run());
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  return (
    <>
      {message && <p role="status">{message}</p>}
      {characters === null && !message && <p aria-busy="true">読み込んでいます…</p>}
      {characters?.length === 0 && <p className="ops-muted">登録されているキャラクタはありません。</p>}
      {characters?.map((c) => {
        const published = c.versions.find((v) => v.status === "published");
        return (
          <article key={c.character_id} className="ops-panel">
            <h3>{c.name}({c.character_id})</h3>
            <div className="ops-table-wrap">
              <table className="ops-table">
                <thead><tr><th scope="col">版</th><th scope="col">状況</th><th scope="col">登録</th><th scope="col">操作</th></tr></thead>
                <tbody>
                  {c.versions.map((v, i) => {
                    const previous = c.versions[i + 1];
                    return (
                      <tr key={v.version}>
                        <td>{v.version_label ?? "—"}(内部 {v.version})</td>
                        <td>{STATUS[v.status] ?? v.status}</td>
                        <td>{formatTime(v.created_at)}</td>
                        <td className="ops-actions">
                          {previous && (
                            <button onClick={async () => {
                              try {
                                const r = await api.personaDiff(c.character_id, previous.version, v.version);
                                setDiff({ title: `${c.character_id}: ${previous.version_label} → ${v.version_label}`, changes: r.changes });
                              } catch (e) {
                                setMessage((e as Error).message);
                              }
                            }}>前の版との差分</button>
                          )}
                          {canPublish && v.status === "deprecated" && v.version_label && (
                            <button onClick={() => window.confirm(`${c.character_id} を ${v.version_label} に戻します。それより新しい公開版は非推奨になります。よろしいですか?`) &&
                              void act(async () => `戻しました(内部の版 ${(await api.rollbackPersona(c.character_id, v.version_label!)).published_version})。`)}>この版に戻す</button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {canPublish && published && (
              <button className="ops-danger" onClick={() => window.confirm(`${c.character_id} を非推奨にします。会話で使えなくなります。よろしいですか?`) &&
                void act(async () => `非推奨にしました(${(await api.deprecatePersona(c.character_id)).deprecated}件)。`)}>非推奨にする</button>
            )}
          </article>
        );
      })}
      {diff && <Changes title={diff.title} changes={diff.changes} onClose={() => setDiff(null)} />}
    </>
  );
}

function Changes({ title, changes, onClose }: { title: string; changes: { path: string; before: unknown; after: unknown }[]; onClose?: () => void }) {
  return (
    <div className="ops-detail" aria-label="差分">
      <div className="ops-toolbar">
        <h3>{title}</h3>
        {onClose && <button className="link" onClick={onClose}>閉じる</button>}
      </div>
      {changes.length === 0 ? <p className="ops-muted">違いはありません。</p> : (
        <div className="ops-table-wrap">
          <table className="ops-table">
            <thead><tr><th scope="col">項目</th><th scope="col">前</th><th scope="col">後</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path}><td><code>{c.path}</code></td><td className="wrap">{show(c.before)}</td><td className="wrap">{show(c.after)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function show(value: unknown): string {
  if (value === undefined || value === null) return "(なし)";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 200 ? `${text.slice(0, 200)}…` : text;
}

// --- 提出・試す ----------------------------------------------------------------------------------

function InspectionView({ inspection }: { inspection: Inspection }) {
  const errors = inspection.findings.filter((f) => f.level === "error");
  const warnings = inspection.findings.filter((f) => f.level === "warning");
  return (
    <div className="ops-inspection" role="status">
      <p className={inspection.ok ? undefined : "ops-error"}>
        {inspection.ok ? "提出できます。" : "提出できません。"} エラー {errors.length}件・警告 {warnings.length}件
        {inspection.scenarios && `・シナリオ ${inspection.scenarios.passed} / ${inspection.scenarios.total} 合格`}
      </p>
      {inspection.blocking.map((b) => <p key={b} className="ops-error">{b}</p>)}
      {[...errors, ...warnings].length > 0 && (
        <ul className="ops-findings">
          {[...errors, ...warnings].map((f, i) => <li key={i}><code>{f.code}</code> {f.message}{f.path && <span className="ops-muted">({f.path})</span>}</li>)}
        </ul>
      )}
      {inspection.scenarios?.failed.map((s) => <p key={s.scenario} className="ops-error">{s.scenario} {s.title}: {s.problems.join(" / ")}</p>)}
    </div>
  );
}

function SubmitAndPreview({ api, canSubmit, canPreview }: { api: OpsApi; canSubmit: boolean; canPreview: boolean }) {
  const [definition, setDefinition] = useState<Record<string, unknown> | null>(null);
  const [fileName, setFileName] = useState("");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function open(file: File) {
    setMessage("");
    setInspection(null);
    try {
      const parsed = (await api.parsePersonaYaml(await file.text())).definition;
      setDefinition(parsed);
      setFileName(file.name);
      setBusy(true);
      setInspection(await api.inspectPersona(parsed));
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p className="ops-muted">デザイナ(/designer)や CLI で作った YAML を開きます。検査(lint と、標準シナリオ・専用シナリオの模擬)の結果を確かめてから、提出します。</p>
      <label className="ops-file">
        YAML を開く
        <input type="file" accept=".yaml,.yml,text/yaml" onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) void open(file);
        }} />
      </label>
      {busy && <p aria-busy="true">検査しています…</p>}
      {message && <p role="alert" className="ops-error">{message}</p>}
      {definition && (
        <p><strong>{String(definition.name ?? "")}</strong>({String(definition.character_id ?? "")}・版 {String(definition.version ?? "")})・{fileName}</p>
      )}
      {inspection && <InspectionView inspection={inspection} />}
      {definition && canSubmit && (
        <button className="primary" disabled={!inspection?.ok || busy} onClick={async () => {
          setMessage("");
          try {
            await api.submitPersona(definition);
            setMessage("提出しました。承認者のレビューを待ちます(提出した本人は承認できません)。");
          } catch (e) {
            setMessage((e as Error).message);
          }
        }}>レビューに提出する</button>
      )}
      {definition && canPreview && inspection?.ok && <LivePreview api={api} definition={definition} />}
    </>
  );
}

function LivePreview({ api, definition }: { api: OpsApi; definition: Record<string, unknown> }) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [log, setLog] = useState<{ who: "user" | "character"; text: string }[]>([]);
  const [text, setText] = useState("");
  const [scenarios, setScenarios] = useState<{ id: string; title: string }[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [simulation, setSimulation] = useState<Awaited<ReturnType<OpsApi["previewSimulate"]>> | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.previewScenarios(String(definition.character_id ?? "")).then((r) => setScenarios(r.scenarios), () => undefined);
    setSessionId(null);
    setLog([]);
  }, [api, definition]);

  const describe = (actions: PreviewAction[]) =>
    actions.map((a) => (a.type === "silence" ? "(無言)" : a.text ?? `(${a.type})`)).join(" / ") || "(応答なし)";

  return (
    <div className="ops-preview">
      <h3>実際のモデルで試す</h3>
      <p className="ops-muted">Azure OpenAI を呼びます(費用がかかります)。会話は画面とサーバーのメモリの中だけで、記録・記憶には残りません。1時間に 60 回まで。</p>
      <div className="ops-chat" aria-label="試しの会話" aria-live="polite">
        {log.map((m, i) => <p key={i} className={`ops-bubble ${m.who}`}>{m.who === "user" ? "あなた: " : "キャラクタ: "}{m.text}</p>)}
      </div>
      <form className="ops-chat-input" onSubmit={async (e) => {
        e.preventDefault();
        if (!text.trim() || busy) return;
        const sent = text.trim();
        setBusy(true);
        setMessage("");
        setText("");
        setLog((l) => [...l, { who: "user", text: sent }]);
        try {
          const r = await api.previewChat(definition, sessionId, sent);
          setSessionId(r.session_id);
          setLog((l) => [...l, { who: "character", text: describe(r.actions) }]);
        } catch (err) {
          setMessage((err as Error).message);
        } finally {
          setBusy(false);
        }
      }}>
        <label className="visually-hidden" htmlFor="ops-preview-text">試しの発話</label>
        <input id="ops-preview-text" value={text} maxLength={1000} onChange={(e) => setText(e.target.value)} placeholder="話しかけてみる" />
        <button className="primary" disabled={busy || !text.trim()}>送る</button>
        <button type="button" onClick={() => { setSessionId(null); setLog([]); }}>最初から</button>
      </form>
      <h3>実際のモデルでのシミュレーション(1〜3件)</h3>
      <div className="ops-checks">
        {scenarios.map((s) => (
          <label key={s.id}>
            <input type="checkbox" checked={chosen.includes(s.id)} disabled={!chosen.includes(s.id) && chosen.length >= 3}
              onChange={(e) => setChosen(e.target.checked ? [...chosen, s.id] : chosen.filter((x) => x !== s.id))} />
            {s.id} {s.title}
          </label>
        ))}
      </div>
      <button disabled={busy || chosen.length === 0} onClick={async () => {
        setBusy(true);
        setMessage("");
        try {
          setSimulation(await api.previewSimulate(definition, chosen));
        } catch (e) {
          setMessage((e as Error).message);
        } finally {
          setBusy(false);
        }
      }}>{busy ? "実行しています…" : "実行する(1時間に5回まで)"}</button>
      {message && <p role="alert" className="ops-error">{message}</p>}
      {simulation?.scenarios.map((s) => (
        <div key={s.scenario} className="ops-panel">
          <h4>{s.scenario} {s.title} — {s.ok ? "合格" : "不合格"}</h4>
          {[...s.violations, ...s.failures].map((p) => <p key={p} className="ops-error">{p}</p>)}
          <ol className="ops-turns">
            {s.turns.map((t) => <li key={t.no}><span className="ops-muted">{t.text}</span> → {t.mode}{t.reply ? `: ${t.reply}` : ""}</li>)}
          </ol>
        </div>
      ))}
    </div>
  );
}

// --- レビュー ------------------------------------------------------------------------------------

function Review({ api, canPublish }: { api: OpsApi; canPublish: boolean }) {
  const [status, setStatus] = useState("pending");
  const [rows, setRows] = useState<PersonaSubmission[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const load = useCallback(async () => {
    try {
      setRows((await api.submissions(status)).submissions);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }, [api, status]);
  useEffect(() => {
    setRows(null);
    setSelected(null);
    void load();
  }, [load]);

  return (
    <>
      <div className="ops-segmented" role="group" aria-label="提出の状況">
        {["pending", "approved", "rejected"].map((s) => (
          <button key={s} aria-pressed={status === s} onClick={() => setStatus(s)}>{STATUS[s]}</button>
        ))}
      </div>
      {message && <p role="alert" className="ops-error">{message}</p>}
      <div className="ops-split">
        <div className="ops-table-wrap">
          {rows === null && <p aria-busy="true">読み込んでいます…</p>}
          {rows?.length === 0 && <p className="ops-muted">該当する提出はありません。</p>}
          {rows && rows.length > 0 && (
            <table className="ops-table">
              <thead><tr><th scope="col">提出</th><th scope="col">キャラクタ</th><th scope="col">版</th><th scope="col">提出者</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.submission_id} className={selected === r.submission_id ? "selected" : undefined}>
                    <td><button className="link" aria-label={`${r.character_id} ${r.version_label} の提出を開く`} onClick={() => setSelected(r.submission_id)}>{formatTime(r.submitted_at)}</button></td>
                    <td>{r.name}({r.character_id})</td>
                    <td>{r.version_label}</td>
                    <td><code>{r.submitted_by.slice(0, 8)}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {selected && <SubmissionView key={selected} api={api} id={selected} canPublish={canPublish} onDone={() => { setSelected(null); void load(); }} />}
      </div>
    </>
  );
}

function SubmissionView({ api, id, canPublish, onDone }: { api: OpsApi; id: string; canPublish: boolean; onDone: () => void }) {
  const [detail, setDetail] = useState<SubmissionDetail | null>(null);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    api.submission(id).then(setDetail, (e) => setMessage((e as Error).message));
  }, [api, id]);
  if (!detail) return <aside className="ops-detail">{message || "読み込んでいます…"}</aside>;
  const warnings = detail.lint_json.filter((f) => f.level === "warning");
  const errors = detail.lint_json.filter((f) => f.level === "error");
  return (
    <aside className="ops-detail" aria-label="提出の詳細">
      <h3>{String(detail.definition.name ?? "")}({detail.character_id})版 {detail.version_label}</h3>
      <dl className="ops-dl">
        <dt>状況</dt><dd>{STATUS[detail.status] ?? detail.status}</dd>
        <dt>提出</dt><dd>{formatTime(detail.submitted_at)}(<code>{detail.submitted_by.slice(0, 8)}</code>)</dd>
        <dt>公開中の版</dt><dd>{detail.current_version ? `${detail.current_version.version_label}(内部 ${detail.current_version.version})` : "なし(初めての公開)"}</dd>
        <dt>検査</dt><dd>エラー {errors.length}件・警告 {warnings.length}件・{detail.simulation_summary}</dd>
        {detail.notes && (<><dt>メモ</dt><dd className="ops-note">{detail.notes}</dd></>)}
      </dl>
      {warnings.length > 0 && <ul className="ops-findings">{warnings.map((f, i) => <li key={i}><code>{f.code}</code> {f.message}</li>)}</ul>}
      <h4>人格と話し方</h4>
      <p className="ops-note">{String(detail.definition.persona_prompt ?? "")}</p>
      <p className="ops-note">{String(detail.definition.speech_style ?? "")}</p>
      {detail.current_version && <Changes title="公開中の版との差分" changes={detail.changes} />}
      {canPublish && detail.status === "pending" && (
        <div className="ops-form">
          <label>
            確認のメモ{warnings.length > 0 ? "(警告があるので必須)" : ""}・差し戻す理由
            <textarea value={notes} maxLength={1000} rows={3} onChange={(e) => setNotes(e.target.value)} />
          </label>
          <div className="ops-actions">
            <button className="primary" onClick={async () => {
              setMessage("");
              try {
                const r = await api.approvePersona(id, notes);
                setMessage(`公開しました(${r.character_id} ${r.version_label}、内部の版 ${r.published_version})。`);
                onDone();
              } catch (e) {
                setMessage((e as Error).message);
              }
            }}>承認して公開する</button>
            <button className="ops-danger" onClick={async () => {
              setMessage("");
              try {
                await api.rejectPersona(id, notes);
                onDone();
              } catch (e) {
                setMessage((e as Error).message);
              }
            }}>差し戻す</button>
          </div>
        </div>
      )}
      {message && <p role="status">{message}</p>}
    </aside>
  );
}
