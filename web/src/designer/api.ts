// デザイナの API(/v1/designer。本体仕様書 §15.1 の M10)。状態を持たない API。下書きはこの画面と端末のファイルだけにある。

import { ApiError, type TokenSource } from "../api";

export type Definition = Record<string, unknown>;
export type Finding = { code: string; level: "error" | "warning" | "info"; message: string; path: string };
export type Traits = Record<string, number>;
export type CurvePoint = {
  relationship: number; talkativeness_eff: number; expressiveness_eff: number; length: string;
  expression_threshold: number; expression_cap: number;
};
export type Resolved = {
  type_id: string | null; type_from_axes: string | null; traits: Traits; defaults: Traits; association: Record<string, number>;
  style_card: string; preferences: { topic: string; affinity: number; source: string }[]; curve: CurvePoint[];
  sync_curve: { relationship: number; weight: number }[]; half_life_turns: number | null;
  sheet_rules: { topic: string; first_delta: number; [key: string]: unknown }[];
};
export type CheckResult = { ok: boolean; findings: Finding[]; uses_sample_id: boolean; resolved: Resolved | null };
export type TypeInfo = { type_id: string; name: string; composition: string; axes: Record<string, number>; style_card: string; defaults: Traits };
export type ProfileInfo = {
  id: string; name: string; deltas: Record<string, number>; initial_relationship: number;
  temperament: { reactivity: number; recovery_rate: number; baseline_vad: { v: number; a: number; d: number } };
};
export type CatalogItem = { id: string; category: string; label: string; likeable: boolean; dislikeable: boolean; involvement: boolean; note: string };
export type Choices = {
  axes: { id: string; name: string; low: string; high: string }[];
  types: TypeInfo[];
  profiles: ProfileInfo[];
  emotions: string[];
  likes_catalog: { version: number; categories: { id: string; label: string }[]; items: CatalogItem[] };
  event_triggers: { name: string; kind: string; label: string; positive_only: boolean; requires_character_target: boolean }[];
};
export type QuizQuestion = { id: string; axis: string; text: string };
export type Quiz = { bank_version: number; preamble: string; disclaimer: string; main: QuizQuestion[]; tiebreak: Record<string, QuizQuestion[]> };
export type AxisResult = { axis: string; answered: number; score: number | null; strength: number | null; pole: string | null; borderline: boolean; used_tiebreak: boolean };
export type QuizResult = {
  type_id: string | null; type_name: string | null; axes: Record<string, AxisResult>; undecided_axes: string[]; warnings: string[];
  base_personality?: Record<"preset" | "scores", Definition>;
};
export type ScenarioTurn = { no: number; text: string; mode: string; length: string | null; strategy: string | null; notices: string[]; fired_rules: string[] };
export type ScenarioReport = { scenario: string; title: string; ok: boolean; violations: string[]; failures: string[]; modes: Record<string, number>; turns: ScenarioTurn[] };
export type SimulationRun = { relationship: number | null; modes: Record<string, number>; passed: number; total: number; scenarios: ScenarioReport[] };
export type SimulationResult = { ok: boolean; runs: SimulationRun[] };
export type SweepRow = {
  type_id: string; profile: string | null; traits: Traits; modes: Record<string, number>; lengths: Record<string, number>;
  violations: string[]; failures: string[]; in_range: boolean;
};

export class DesignerApi {
  private readonly baseUrl: string;
  private readonly token: TokenSource;

  constructor(baseUrl: string, token: TokenSource) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  private async request<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${this.baseUrl}/v1/designer${path}`, {
      method,
      headers: { Authorization: `Bearer ${await this.token()}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
    if (!response.ok) {
      let message = "通信に失敗しました。少し待って、もう一度お試しください。";
      try {
        const detail = ((await response.json()) as { detail?: unknown }).detail;
        if (typeof detail === "string") message = detail;
      } catch {
        // 本文が JSON でないときは、既定の文面
      }
      if (response.status === 401) message = "サインインの有効期限が切れました。もう一度サインインしてください。";
      throw new ApiError(response.status, "designer_error", message);
    }
    return (await response.json()) as T;
  }

  choices(): Promise<Choices> {
    return this.request("GET", "/choices");
  }

  samples(): Promise<{ samples: { character_id: string; name: string }[] }> {
    return this.request("GET", "/samples");
  }

  sample(id: string): Promise<{ character_id: string; read_only: boolean; definition: Definition; yaml: string }> {
    return this.request("GET", `/samples/${encodeURIComponent(id)}`);
  }

  create(body: { character_id: string; name: string; type_id: string; profile: string | null }): Promise<{ definition: Definition }> {
    return this.request("POST", "/new", body);
  }

  parseYaml(text: string): Promise<{ definition: Definition }> {
    return this.request("POST", "/yaml/parse", { text });
  }

  dumpYaml(definition: Definition): Promise<{ yaml: string }> {
    return this.request("POST", "/yaml/dump", { definition });
  }

  check(definition: Definition, signal?: AbortSignal): Promise<CheckResult> {
    return this.request("POST", "/check", { definition }, signal);
  }

  quiz(): Promise<Quiz> {
    return this.request("GET", "/quiz");
  }

  scoreQuiz(answers: Record<string, number>, tiebreakChoice: Record<string, string>): Promise<QuizResult> {
    return this.request("POST", "/quiz/score", { answers, tiebreak_choice: tiebreakChoice });
  }

  simulate(definition: Definition, relationships: (number | null)[], under18: boolean): Promise<SimulationResult> {
    return this.request("POST", "/simulate", { definition, relationships, under_18: under18 });
  }

  sweep(): Promise<{ rows: SweepRow[] }> {
    return this.request("GET", "/sweep");
  }
}
