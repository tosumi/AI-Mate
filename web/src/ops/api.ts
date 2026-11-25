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

export type PersonaVersion = { version: number; version_label: string | null; status: string; created_at: string };
export type PersonaCharacter = { character_id: string; name: string; versions: PersonaVersion[] };
export type PersonaSubmission = {
  submission_id: string; character_id: string; version_label: string; name: string | null; submitted_by: string; submitted_at: string;
  status: "pending" | "approved" | "rejected"; decided_by: string | null; decided_at: string | null; published_version: number | null;
};
export type PersonaFinding = { code: string; level: string; message: string; path: string };
export type Inspection = {
  ok: boolean; findings: PersonaFinding[]; blocking: string[];
  scenarios: { passed: number; total: number; failed: { scenario: string; title: string; problems: string[] }[]; modes: Record<string, number> } | null;
};
export type SubmissionDetail = PersonaSubmission & {
  definition: Record<string, unknown>; lint_json: PersonaFinding[]; simulation_summary: string; notes: string | null;
  current_version: { version: number; version_label: string | null } | null; changes: { path: string; before: unknown; after: unknown }[];
};
export type PreviewAction = { type: string; text?: string };

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
    if (!response.ok) {
      // 運営者に伝えてよい理由(提出した本人は承認できない、など)は、サーバーの文面を使う
      let detail: unknown;
      try {
        detail = ((await response.json()) as { detail?: unknown }).detail;
      } catch {
        detail = undefined;
      }
      throw toError(response.status, typeof detail === "string" && [403, 409, 422, 429].includes(response.status) ? detail : undefined);
    }
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

  personas(): Promise<{ characters: PersonaCharacter[]; pending: PersonaSubmission[] }> {
    return this.request("GET", "/personas");
  }

  submissions(status: string): Promise<{ submissions: PersonaSubmission[] }> {
    return this.request("GET", `/personas/submissions?${query({ status })}`);
  }

  submission(id: string): Promise<SubmissionDetail> {
    return this.request("GET", `/personas/submissions/${encodeURIComponent(id)}`);
  }

  parsePersonaYaml(text: string): Promise<{ definition: Record<string, unknown> }> {
    return this.request("POST", "/personas/yaml", { text });
  }

  inspectPersona(definition: Record<string, unknown>): Promise<Inspection> {
    return this.request("POST", "/personas/inspect", { definition });
  }

  submitPersona(definition: Record<string, unknown>): Promise<{ submission_id: string; inspection: Inspection }> {
    return this.request("POST", "/personas/submissions", { definition });
  }

  approvePersona(id: string, notes: string): Promise<{ published_version: number; character_id: string; version_label: string }> {
    return this.request("POST", `/personas/submissions/${encodeURIComponent(id)}/approve`, { notes: notes || null });
  }

  rejectPersona(id: string, notes: string): Promise<{ rejected: boolean }> {
    return this.request("POST", `/personas/submissions/${encodeURIComponent(id)}/reject`, { notes: notes || null });
  }

  rollbackPersona(characterId: string, to: string): Promise<{ published_version: number }> {
    return this.request("POST", `/personas/${encodeURIComponent(characterId)}/rollback`, { to });
  }

  deprecatePersona(characterId: string): Promise<{ deprecated: number }> {
    return this.request("POST", `/personas/${encodeURIComponent(characterId)}/deprecate`);
  }

  personaDiff(characterId: string, first: number, second: number): Promise<{ changes: { path: string; before: unknown; after: unknown }[] }> {
    return this.request("GET", `/personas/${encodeURIComponent(characterId)}/diff?${query({ first: String(first), second: String(second) })}`);
  }

  previewScenarios(characterId?: string): Promise<{ scenarios: { id: string; title: string }[] }> {
    return this.request("GET", `/personas/scenarios?${query({ character_id: characterId })}`);
  }

  previewChat(definition: Record<string, unknown>, sessionId: string | null, text: string): Promise<{ session_id: string; actions: PreviewAction[] }> {
    return this.request("POST", "/personas/preview/chat", { definition, session_id: sessionId, text });
  }

  previewSimulate(definition: Record<string, unknown>, scenarios: string[]): Promise<{ scenarios: { scenario: string; title: string; ok: boolean; violations: string[]; failures: string[]; turns: { no: number; text: string; mode: string; reply?: string | null }[] }[]; modes: Record<string, number> }> {
    return this.request("POST", "/personas/preview/simulate", { definition, scenarios });
  }
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) search.set(key, value);
  return search.toString();
}

// サーバーの文面ではなく、状態に応じた文面を出す(運営者の画面の文面を、ここでそろえる)
function toError(status: number, detail?: string): ApiError {
  const messages: Record<number, string> = {
    401: "サインインの有効期限が切れました。もう一度サインインしてください。",
    403: "この操作の権限がありません。",
    404: "見つかりませんでした。",
    409: "ほかの操作と重なったか、すでに処理されています。",
    429: "回数が多すぎます。しばらく待ってから、もう一度お試しください。",
    422: "入力の内容が正しくありません。",
    503: "いまは使えません。少し待って、もう一度お試しください。",
  };
  return new ApiError(status, "ops_error", detail ?? messages[status] ?? "通信に失敗しました。少し待って、もう一度お試しください。");
}
