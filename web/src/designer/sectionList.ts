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

