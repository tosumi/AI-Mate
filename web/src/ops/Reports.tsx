// 報告の確認(サポート・管理者。本体仕様書 §15.1 の M9)。
// 会話の本文は出さない(サーバーも返さない)。本人が報告に書いたメモと、ガードレールの記録だけを見る。
// ユーザーは仮の ID で示す。対応状況・対応メモは、handle_reports の権限があるときだけ変えられる。

import { useCallback, useEffect, useState } from "react";
import type { OpsApi, ReportDetail, ReportStatus, ReportSummary } from "./api";
import { formatTime, label, REASON_LABELS, STATUS_LABELS } from "./labels";

const MEMO_MAX = 500;
const FILTERS: { id: ReportStatus | ""; label: string }[] = [
  { id: "open", label: "未対応" },
  { id: "in_progress", label: "対応中" },
  { id: "resolved", label: "対応済み" },
  { id: "", label: "すべて" },
];

export function Reports({ api, canHandle }: { api: OpsApi; canHandle: boolean }) {
  const [filter, setFilter] = useState<ReportStatus | "">("open");
  const [reports, setReports] = useState<ReportSummary[] | null>(null);
  const [more, setMore] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async (status: ReportStatus | "", before?: string) => {
    setError("");
    try {
      const { reports: page } = await api.reports(status, before);
      setReports((current) => (before ? [...(current ?? []), ...page] : page));
      setMore(page.length >= 50);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [api]);

  useEffect(() => {
    setReports(null);
    setSelected(null);
    void load(filter);
  }, [load, filter]);

  const onUpdated = (detail: ReportDetail) => {
    setReports((current) => current?.map((r) => (r.report_id === detail.report_id ? { ...r, status: detail.status, ops_updated_at: detail.ops_updated_at } : r)) ?? null);
  };

  return (
    <section className="ops-section" aria-labelledby="ops-reports-title">
      <div className="ops-toolbar">
        <h2 id="ops-reports-title">報告</h2>
        <div className="ops-segmented" role="group" aria-label="対応状況">
          {FILTERS.map((f) => (
            <button key={f.id || "all"} aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}>{f.label}</button>
          ))}
        </div>
        <button onClick={() => void load(filter)}>読み直す</button>
      </div>
      {error && <p role="alert" className="ops-error">{error}</p>}
      <div className="ops-split">
        <div className="ops-table-wrap">
          {reports === null && !error && <p aria-busy="true">読み込んでいます…</p>}
          {reports?.length === 0 && <p className="ops-muted">該当する報告はありません。</p>}
          {reports && reports.length > 0 && (
            <table className="ops-table">
              <caption className="visually-hidden">報告の一覧(新しい順)</caption>
              <thead>
                <tr><th scope="col">受付</th><th scope="col">理由</th><th scope="col">キャラクタ</th><th scope="col">ユーザー</th><th scope="col">状況</th></tr>
              </thead>
              <tbody>
                {reports.map((r) => (
                  <tr key={r.report_id} className={selected === r.report_id ? "selected" : undefined}>
                    <td>
                      <button className="link" aria-label={`${formatTime(r.created_at)} の報告を開く`} onClick={() => setSelected(r.report_id)}>
                        {formatTime(r.created_at)}
                      </button>
                    </td>
                    <td>{label(REASON_LABELS, r.reason)}{r.has_note && <span className="ops-chip">メモあり</span>}</td>
                    <td>{r.character_id}</td>
                    <td><code>{r.user}</code></td>
                    <td><span className={`ops-status ${r.status}`}>{label(STATUS_LABELS, r.status)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {more && reports && (
            <button onClick={() => void load(filter, reports[reports.length - 1]?.created_at)}>さらに読み込む</button>
          )}
        </div>
        {selected && <ReportView key={selected} api={api} id={selected} canHandle={canHandle} onUpdated={onUpdated} onClose={() => setSelected(null)} />}
      </div>
    </section>
  );
}

function ReportView({ api, id, canHandle, onUpdated, onClose }: {
  api: OpsApi; id: string; canHandle: boolean; onUpdated: (d: ReportDetail) => void; onClose: () => void;
}) {
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [status, setStatus] = useState<ReportStatus>("open");
  const [memo, setMemo] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    api.report(id).then(
      (d) => {
        if (!alive) return;
        setDetail(d);
        setStatus(d.status);
        setMemo(d.ops_memo ?? "");
      },
      (e) => alive && setMessage((e as Error).message),
    );
    return () => {
      alive = false;
    };
  }, [api, id]);

  async function save() {
    if (!detail) return;
    const changes: { status?: ReportStatus; memo?: string } = {};
    if (status !== detail.status) changes.status = status;
    if (memo !== (detail.ops_memo ?? "")) changes.memo = memo;
    if (Object.keys(changes).length === 0) {
      setMessage("変更はありません。");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const updated = await api.updateReport(id, changes);
      setDetail({ ...updated, guardrails: detail.guardrails }); // 更新の応答には、ガードレールの記録を含めない
      onUpdated(updated);
      setMessage("保存しました。");
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  const g = detail?.guardrails;
  return (
    <aside className="ops-detail" aria-label="報告の詳細">
      <div className="ops-toolbar">
        <h3>報告の詳細</h3>
        <button className="link" onClick={onClose}>閉じる</button>
      </div>
      {!detail && !message && <p aria-busy="true">読み込んでいます…</p>}
      {detail && (
        <>
          <dl className="ops-dl">
            <dt>受付</dt><dd>{formatTime(detail.created_at)}</dd>
            <dt>理由</dt><dd>{label(REASON_LABELS, detail.reason)}</dd>
            <dt>キャラクタ</dt><dd>{detail.character_id}</dd>
            <dt>ユーザー(仮の ID)</dt><dd><code>{detail.user}</code></dd>
            <dt>対象のターン</dt><dd>{detail.turn_no ?? "—"}</dd>
            <dt>本人のメモ</dt><dd className="ops-note">{detail.note || "(なし)"}</dd>
          </dl>
          <h4>ガードレールの記録</h4>
          {!g && <p className="ops-muted">対象のターンの記録はありません(保持期間を過ぎたか、ターンの指定がありません)。</p>}
          {g && (
            <>
              <dl className="ops-dl">
                <dt>応答の種類</dt><dd>{g.response_mode ?? "—"}</dd>
                <dt>感情フィルタ</dt><dd>{g.filter_mode ?? "—"}</dd>
                <dt>安全のフラグ</dt><dd>{g.safety_flags?.length ? g.safety_flags.join(", ") : "なし"}</dd>
                <dt>ハードルール</dt><dd>{formatList(g.gate_rule_hits_json)}</dd>
                <dt>システム通知</dt><dd>{formatList(g.system_notices)}</dd>
                <dt>無言の理由</dt><dd>{g.silence_reason ?? "—"}</dd>
                <dt>作り方・モデル</dt><dd>{[g.compose_mode, g.reply_model, g.reply_prompt_version].filter(Boolean).join(" / ") || "—"}</dd>
                <dt>キャラクタの版</dt><dd>{g.character_version ?? "—"}</dd>
              </dl>
              <h4>スクリーニング</h4>
              {g.screening.length === 0 ? (
                <p className="ops-muted">検出はありません。</p>
              ) : (
                <div className="ops-table-wrap">
                  <table className="ops-table">
                    <thead>
                      <tr><th scope="col">層</th><th scope="col">分類</th><th scope="col">重大度</th><th scope="col">検出器</th><th scope="col">スコア</th><th scope="col">処置</th></tr>
                    </thead>
                    <tbody>
                      {g.screening.map((f, i) => (
                        <tr key={i}><td>{f.layer}</td><td>{f.category}</td><td>{f.severity}</td><td>{f.detector}</td><td>{f.score ?? "—"}</td><td>{f.action}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
          <h4>対応</h4>
          {canHandle ? (
            <div className="ops-form">
              <label>
                対応状況
                <select value={status} onChange={(e) => setStatus(e.target.value as ReportStatus)}>
                  {Object.entries(STATUS_LABELS).map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                </select>
              </label>
              <label>
                対応メモ(会話の本文・個人を特定できる情報は書かない)
                <textarea value={memo} maxLength={MEMO_MAX} rows={4} onChange={(e) => setMemo(e.target.value)} />
              </label>
              <span className="ops-muted">{memo.length} / {MEMO_MAX}</span>
              <button className="primary" disabled={saving} onClick={() => void save()}>保存</button>
            </div>
          ) : (
            <dl className="ops-dl">
              <dt>対応状況</dt><dd>{label(STATUS_LABELS, detail.status)}</dd>
              <dt>対応メモ</dt><dd className="ops-note">{detail.ops_memo || "(なし)"}</dd>
            </dl>
          )}
          {detail.ops_updated_at && <p className="ops-muted">最終更新: {formatTime(detail.ops_updated_at)}</p>}
        </>
      )}
      {message && <p role="status">{message}</p>}
    </aside>
  );
}

function formatList(value: unknown): string {
  if (Array.isArray(value)) return value.length ? value.map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join(", ") : "なし";
  if (value == null) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}
