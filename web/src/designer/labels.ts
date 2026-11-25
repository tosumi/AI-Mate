// デザイナの画面の表示の文言。

export const TRAIT_LABELS: Record<string, string> = {
  introversion: "内向度",
  talkativeness: "口数",
  articulation: "表現力",
  expressiveness: "感情の表出",
  initiative: "自発性",
  relationship_sensitivity: "関係値への感応度",
};
export const TRAIT_KEYS = Object.keys(TRAIT_LABELS);
export const DELTA_KEYS = TRAIT_KEYS.filter((k) => k !== "introversion");

export const ASSOCIATION_LABELS: Record<string, string> = { association_ease: "連想のしやすさ", topic_shift: "話題の移りやすさ" };

export const EMOTION_LABELS: Record<string, string> = {
  joy: "喜び", trust: "信頼", fear: "恐れ", surprise: "驚き",
  sadness: "悲しみ", disgust: "嫌悪", anger: "怒り", anticipation: "期待",
};

export const CONCEPT_FIELDS: [string, string][] = [
  ["one_line", "ひとことで"],
  ["role", "役割"],
  ["core_trait", "核になる特徴"],
  ["contrast", "意外な一面"],
  ["relationship_start", "関係の始まり"],
  ["avoid", "避けること"],
];

export const ADJUSTABLE_LABELS: Record<string, string> = {
  tone_softness: "口調のやわらかさ", distance: "距離感", talk_amount: "話す量", question_frequency: "質問の多さ", humor: "ユーモア",
};
export const FIXED_LABELS: Record<string, string> = {
  first_person: "一人称", speech_endings: "語尾", catchphrases: "口癖", core_values: "価値観", character_name: "名前",
};

export const STRENGTH_LABELS: Record<string, string> = { weak: "弱い", medium: "ふつう", strong: "強い" };
export const REPEAT_LABELS: Record<string, string> = { habituate: "慣れる", sensitize: "敏感になる", flat: "毎回同じ" };
export const LENGTH_LABELS: Record<string, string> = { short: "短い", medium: "ふつう", long: "長い" };
export const MODE_LABELS: Record<string, string> = {
  full: "ふつう", brief: "短め", backchannel: "相づち", silence: "無言", notice: "通知", restricted: "制限",
};
export const VAD_LABELS: Record<string, string> = { v: "快・不快", a: "覚醒", d: "支配" };

export function signed(value: number, digits = 2): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}
