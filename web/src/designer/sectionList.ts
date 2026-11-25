// 設定の項目の一覧と型(「デザイナ」と「ウィザード」で共通)。

import type { Choices, Definition, DesignerApi } from "./api";

export type Edit = (path: (string | number)[], value: unknown) => void;
export type SectionProps = { def: Definition; edit: Edit; choices: Choices; api: DesignerApi };

export type SectionId = "basic" | "type" | "expression" | "temperament" | "likes" | "events" | "topics" | "persona";
export const SECTIONS: { id: SectionId; label: string; hint: string }[] = [
  { id: "basic", label: "基本", hint: "ID・名前・コンセプト" },
  { id: "type", label: "性格タイプ", hint: "16タイプ・診断・軸" },
  { id: "expression", label: "表出", hint: "口数・表出の調整、関係値" },
  { id: "temperament", label: "気質", hint: "感情の動きやすさ、無言" },
  { id: "likes", label: "好き嫌い", hint: "好みカタログから選ぶ" },
  { id: "events", label: "イベント", hint: "感謝・あいさつなどへの反応" },
  { id: "topics", label: "話題ルール", hint: "話題と嗜好" },
  { id: "persona", label: "人格・話し方", hint: "性格ディレクティブ、話し方" },
];

// 検査の結果の場所(lint の path)から、設定の項目を決める(先頭の鍵で決まる)
const SECTION_BY_KEY: Record<string, SectionId> = {
  character_id: "basic", name: "basic", version: "basic", status: "basic", concept: "basic", schema_version: "basic",
  base_personality: "type",
  profile: "expression", overrides: "expression", initial_relationship: "expression", adaptation: "expression",
  temperament: "temperament", silence_policy: "temperament",
  likes_dislikes: "likes",
  event_rules: "events",
  appraisal_rules: "topics", preferences: "topics",
  persona_prompt: "persona", speech_style: "persona",
};

/** lint の path を、画面の data-path の形にする(`event_rules[2]` → `event_rules.2`)。 */
export function normalizePath(path: string): string {
  return path.replace(/\[(\d+)\]/g, ".$1");
}

export function sectionForPath(path: string): SectionId | null {
  const key = normalizePath(path).split(".")[0];
  return SECTION_BY_KEY[key] ?? null;
}

/** 移る先の候補(詳しい場所から順に、親へ)。`a.b.c` → [`a.b.c`, `a.b`, `a`]。 */
export function pathCandidates(path: string): string[] {
  const parts = normalizePath(path).split(".");
  return parts.map((_, i) => parts.slice(0, parts.length - i).join("."));
}
