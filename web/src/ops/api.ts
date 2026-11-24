// 運営者の API(/ops/v1。本体仕様書 §15.1 の M9)の呼び出し。トークンは Authorization ヘッダーで送る。
// 応答には、会話の本文は含まれない(サーバーが、返してよい項目だけを選ぶ)。

import { ApiError, type TokenSource } from "../api";

export type Me = { roles: string[]; permissions: string[] };
export type ReportStatus = "open" | "in_progress" | "resolved";
export type ReportSummary = {
  report_id: string; user: string; character_id: string; session_id: string | null; turn_no: number | null;
  reason: string; has_note: boolean; status: ReportStatus; created_at: string; ops_updated_at: string | null;
};
export type Finding = { layer: string; category: string; severity: string; detector: string; score: number | null; action: string };
export type Guardrails = {
  ts: string | null; response_mode: string | null; filter_mode: string | null; safety_flags: string[] | null;
  silence_reason: string | null; gate_rule_hits_json: unknown; system_notices: unknown;
  character_version: number | null; compose_mode: string | null; reply_model: string | null; reply_prompt_version: string | null;
  screening: Finding[];
};
export type ReportDetail = ReportSummary & {
  note: string | null; ops_memo: string | null; ops_updated_by: string | null; guardrails: Guardrails | null;
};
export type RatingRow = {
  character_id: string; character_version: number | null; compose_mode: string | null; reply_model: string | null;
  reply_prompt_version: string | null; response_mode: string | null; raters: number; n: number;
  overall: number | null; persona_fit: number | null; naturalness: number | null; empathy: number | null; low: number | null;
};
export type AuditEntry = {
  log_id: string; ts: string; actor_type: string; actor_id: string | null; action: string;
  target_user: string | null; details: Record<string, unknown>;
};
export type Period = "24h" | "7d" | "30d";
export type MetricSeries = { id: string; label: string; unit: string; points: [string, number][] };
export type MetricPanel = { id: string; title: string; configured: boolean; available: boolean; series: MetricSeries[] };
export type SystemMetrics = { period: Period; interval: string; start: string; end: string; generated_at: string; panels: MetricPanel[] };

export class OpsApi {
  private readonly baseUrl: string;
  private readonly token: TokenSource;

  constructor(baseUrl: string, token: TokenSource) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await fetch(`${this.baseUrl}/ops/v1${path}`, {
      method,
      headers: { Authorization: `Bearer ${await this.token()}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw toError(response.status);
    return (await response.json()) as T;
  }

  me(): Promise<Me> {
    return this.request("GET", "/me");
  }

  reports(status: ReportStatus | "", before?: string): Promise<{ reports: ReportSummary[] }> {
    return this.request("GET", `/reports?${query({ status, before, limit: "50" })}`);
  }

  report(id: string): Promise<ReportDetail> {
    return this.request("GET", `/reports/${encodeURIComponent(id)}`);
  }

  updateReport(id: string, changes: { status?: ReportStatus; memo?: string }): Promise<ReportDetail> {
    return this.request("PATCH", `/reports/${encodeURIComponent(id)}`, changes);
  }

  ratings(since: string, minN: number): Promise<{ since: string; min_n: number; rows: RatingRow[] }> {
    return this.request("GET", `/ratings?${query({ since, min_n: String(minN) })}`);
  }

  auditLogs(filters: { actorType: string; since?: string; before?: string }): Promise<{ entries: AuditEntry[] }> {
    return this.request("GET", `/audit-logs?${query({ actor_type: filters.actorType, since: filters.since, before: filters.before, limit: "100" })}`);
  }

  systemMetrics(period: Period): Promise<SystemMetrics> {
    return this.request("GET", `/system/metrics?${query({ period })}`);
  }
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) search.set(key, value);
  return search.toString();
}

// サーバーの文面ではなく、状態に応じた文面を出す(運営者の画面の文面を、ここでそろえる)
function toError(status: number): ApiError {
  const messages: Record<number, string> = {
    401: "サインインの有効期限が切れました。もう一度サインインしてください。",
    403: "この操作の権限がありません。",
    404: "見つかりませんでした。",
    422: "入力の内容が正しくありません。",
    503: "いまは使えません。少し待って、もう一度お試しください。",
  };
  return new ApiError(status, "ops_error", messages[status] ?? "通信に失敗しました。少し待って、もう一度お試しください。");
}
