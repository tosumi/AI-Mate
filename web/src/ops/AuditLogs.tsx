// 監査ログの閲覧(管理者・監査。本体仕様書 §15.1 の M9)。新しい順。ユーザーは仮の ID で示す。

import { useCallback, useEffect, useState } from "react";
import type { AuditEntry, OpsApi } from "./api";
import { ACTOR_LABELS, DECISION_LABELS, formatTime, label, ROLE_LABELS, STATUS_LABELS } from "./labels";

const ACTORS = [
  { id: "", label: "すべて" },
  { id: "operator", label: "運営者" },
  { id: "user", label: "ユーザー" },
  { id: "system", label: "システム" },
];
const RANGES = [
  { hours: 24, label: "24時間" },
  { hours: 24 * 7, label: "7日" },
  { hours: 24 * 30, label: "30日" },
];
const PAGE = 100;
const OUTCOMES: Record<string, string> = { requested: "要求", done: "済み", refused: "断った" };

export function AuditLogs({ api }: { api: OpsApi }) {
  const [actorType, setActorType] = useState("");
  const [hours, setHours] = useState(24);
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (before?: string) => {
    setError("");
    try {
      const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
      const { entries: page } = await api.auditLogs({ actorType, since, before });
      setEntries((current) => (before ? [...(current ?? []), ...page] : page));
      setMore(page.length >= PAGE);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [api, actorType, hours]);

  useEffect(() => {
    setEntries(null);
    void load();
  }, [load]);

  return (
    <section className="ops-section" aria-labelledby="ops-audit-title">
      <div className="ops-toolbar">
        <h2 id="ops-audit-title">監査ログ</h2>
        <div className="ops-segmented" role="group" aria-label="操作した人">
          {ACTORS.map((a) => (
            <button key={a.id || "all"} aria-pressed={actorType === a.id} onClick={() => setActorType(a.id)}>{a.label}</button>
          ))}
        </div>
        <div className="ops-segmented" role="group" aria-label="期間">
          {RANGES.map((r) => (
            <button key={r.hours} aria-pressed={hours === r.hours} onClick={() => setHours(r.hours)}>{r.label}</button>
          ))}
        </div>
        <button onClick={() => void load()}>読み直す</button>
      </div>
      {error && <p role="alert" className="ops-error">{error}</p>}
      {entries === null && !error && <p aria-busy="true">読み込んでいます…</p>}
      {entries?.length === 0 && <p className="ops-muted">この条件の記録はありません。</p>}
      {entries && entries.length > 0 && (
        <div className="ops-table-wrap">
          <table className="ops-table">
            <thead>
              <tr><th scope="col">日時</th><th scope="col">種類</th><th scope="col">操作した人</th><th scope="col">操作</th><th scope="col">対象</th><th scope="col">詳細</th></tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.log_id}>
                  <td className="nowrap">{formatTime(e.ts)}</td>
                  <td>{label(ACTOR_LABELS, e.actor_type)}</td>
                  <td><code>{e.actor_id ?? "—"}</code></td>
                  <td><code>{e.action}</code></td>
                  <td>{e.target_user ? <code>{e.target_user}</code> : "—"}</td>
                  <td className="wrap">{describe(e.details)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {more && entries && <button onClick={() => void load(entries[entries.length - 1]?.ts)}>さらに読み込む</button>}
    </section>
  );
}

function describe(details: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof details.decision === "string") parts.push(label(DECISION_LABELS, details.decision));
  if (Array.isArray(details.roles) && details.roles.length) parts.push(`ロール: ${details.roles.map((r) => label(ROLE_LABELS, String(r))).join("・")}`);
  if (typeof details.permission === "string") parts.push(`権限: ${details.permission}`);
  if (details.target && typeof details.target === "object") {
    parts.push(`対象: ${Object.entries(details.target as Record<string, unknown>).map(([k, v]) => `${k}=${String(v)}`).join(", ")}`);
  }
  if (typeof details.report_id === "string") parts.push(`報告: ${details.report_id}`);
  if (typeof details.status === "string") parts.push(`状況: ${label(STATUS_LABELS, details.status)}`);
  if (details.memo_changed === true) parts.push("メモを変更");
  // キャラクタの提出・公開(§15.1 の M10)
  if (typeof details.character_id === "string") parts.push(`キャラクタ: ${details.character_id}${typeof details.version_label === "string" ? ` ${details.version_label}` : ""}`);
  if (typeof details.submission_id === "string") parts.push(`提出: ${details.submission_id.slice(0, 8)}`);
  if (typeof details.outcome === "string") parts.push(`結果: ${OUTCOMES[details.outcome.split(":")[0]] ?? details.outcome}${details.outcome.includes(":") ? `(${details.outcome.split(":")[1]})` : ""}`);
  return parts.join(" / ") || "—";
}
