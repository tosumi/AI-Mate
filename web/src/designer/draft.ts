// 下書き(編集中の定義)の操作と、タブの一時保存領域への自動の保存(本体仕様書 §15.1 の M10)。
// 下書きはサーバーに保存しない。タブを閉じると消える(残すときは「YAML を保存」で端末に保存する)。

import type { Definition } from "./api";

const DRAFT_KEY = "mycompanion.designer.draft";
const MODE_KEY = "mycompanion.designer.mode";

export type Draft = {
  definition: Definition;
  baseline: Definition; // 開いたとき・最後に保存したときの定義(「編集前と編集後」の比較に使う)
  source: string; // どこから開いたか(表示用)
};

export type Mode = "designer" | "wizard";

export function loadDraft(): Draft | null {
  try {
    const text = sessionStorage.getItem(DRAFT_KEY);
    if (!text) return null;
    const draft = JSON.parse(text) as Draft;
    return draft && typeof draft.definition === "object" ? draft : null;
  } catch {
    return null;
  }
}

export function saveDraft(draft: Draft | null): void {
  try {
    if (draft) sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    else sessionStorage.removeItem(DRAFT_KEY);
  } catch {
    // 保存できない(容量・プライベートモード)ときは、残さないだけ
  }
}

export function loadMode(): Mode | null {
  try {
    const value = sessionStorage.getItem(MODE_KEY);
    return value === "designer" || value === "wizard" ? value : null;
  } catch {
    return null;
  }
}

export function saveMode(mode: Mode): void {
  try {
    sessionStorage.setItem(MODE_KEY, mode);
  } catch {
    // 残さないだけ
  }
}

/** path の値を読む(途中がなければ undefined)。 */
export function getIn(obj: unknown, path: (string | number)[]): unknown {
  let current: unknown = obj;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<string | number, unknown>)[key];
  }
  return current;
}

/** path の値を変えた、新しいオブジェクト(元は変えない)。undefined を入れると、その鍵を消す。 */
export function setIn<T>(obj: T, path: (string | number)[], value: unknown): T {
  if (path.length === 0) return value as T;
  const [key, ...rest] = path;
  const base = (obj ?? (typeof key === "number" ? [] : {})) as Record<string | number, unknown> | unknown[];
  const copy: Record<string | number, unknown> | unknown[] = Array.isArray(base) ? [...base] : { ...base };
  const next = setIn((base as Record<string | number, unknown>)[key], rest, value);
  if (next === undefined && !Array.isArray(copy)) delete (copy as Record<string | number, unknown>)[key];
  else (copy as Record<string | number, unknown>)[key] = next;
  return copy as T;
}

/** 2つの定義の違い(変わった葉の path と、前後の値)。比較の「編集前と編集後」に使う。 */
export function diffDefinitions(before: unknown, after: unknown, prefix = ""): { path: string; before: unknown; after: unknown }[] {
  const isObject = (v: unknown) => v !== null && typeof v === "object" && !Array.isArray(v);
  if (isObject(before) && isObject(after)) {
    const keys = [...new Set([...Object.keys(before as object), ...Object.keys(after as object)])].sort();
    return keys.flatMap((key) =>
      diffDefinitions((before as Record<string, unknown>)[key], (after as Record<string, unknown>)[key], prefix ? `${prefix}.${key}` : key),
    );
  }
  return JSON.stringify(before) === JSON.stringify(after) ? [] : [{ path: prefix, before, after }];
}

/** ファイル名(キャラクタの ID から。端末に保存するとき)。 */
export function fileName(definition: Definition): string {
  const id = typeof definition.character_id === "string" && definition.character_id ? definition.character_id : "persona";
  return `${id.replace(/[^A-Za-z0-9_]/g, "_")}.persona.yaml`;
}

export const SAMPLE_PREFIX = "sample_";
