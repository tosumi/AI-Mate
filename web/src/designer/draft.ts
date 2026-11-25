// 下書き(編集中の定義)の操作と、タブの一時保存領域への自動の保存(本体仕様書 §15.1 の M10)。
// 下書きはサーバーに保存しない。タブを閉じると消える(残すときは「YAML を保存」で端末に保存する)。

import type { Definition, WizardAnswer } from "./api";

const DRAFT_KEY = "mycompanion.designer.draft";
const WIZARD_KEY = "mycompanion.designer.wizard";

export type Draft = {
  definition: Definition;
  baseline: Definition; // 開いたとき・最後に保存したときの定義(「編集前と編集後」の比較に使う)
  source: string; // どこから開いたか(表示用)
};

export type Mode = "designer" | "wizard";

/** ウィザードの途中(答えた順の回答)。再読み込みしても続きから答えられるように、タブの一時保存領域に残す。 */
export type WizardState = { character_id: string; name: string; answers: [string, WizardAnswer][] };

function load<T>(key: string, ok: (value: T) => boolean): T | null {
  try {
    const text = sessionStorage.getItem(key);
    if (!text) return null;
    const value = JSON.parse(text) as T;
    return value && ok(value) ? value : null;
  } catch {
    return null;
  }
}

function save(key: string, value: unknown): void {
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch {
    // 保存できない(容量・プライベートモード)ときは、残さないだけ
  }
}

export function loadDraft(): Draft | null {
  return load<Draft>(DRAFT_KEY, (d) => typeof d.definition === "object");
}

export function saveDraft(draft: Draft | null): void {
  save(DRAFT_KEY, draft);
}

export function loadWizard(): WizardState | null {
  return load<WizardState>(WIZARD_KEY, (w) => typeof w.character_id === "string" && Array.isArray(w.answers));
}

export function saveWizard(state: WizardState | null): void {
  save(WIZARD_KEY, state);
}

/** 自分のキャラクタの ID として使えるか(英小文字で始まる 2〜41 文字。サンプルの ID は使えない)。 */
export function isOwnId(id: string): boolean {
  return /^[a-z][a-z0-9_]{1,40}$/.test(id) && !id.startsWith(SAMPLE_PREFIX);
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
