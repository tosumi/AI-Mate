// 運営者の画面の表示の文言と、値の書式。

export const ROLE_LABELS: Record<string, string> = {
  designer: "設計者",
  approver: "承認者",
  support: "サポート",
  admin: "管理者",
  auditor: "監査",
};

export const STATUS_LABELS: Record<string, string> = { open: "未対応", in_progress: "対応中", resolved: "対応済み" };

export const REASON_LABELS: Record<string, string> = {
  inappropriate: "不適切",
  harmful: "有害",
  incorrect: "誤り",
  other: "その他",
};

export const ACTOR_LABELS: Record<string, string> = { user: "ユーザー", system: "システム", operator: "運営者" };

export const DECISION_LABELS: Record<string, string> = { allowed: "許可", denied: "拒否", unauthenticated: "未認証" };

export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (value == null) return "—";
  return map[value] ?? value;
}

/** 日時(ブラウザの地域の時刻で、秒まで)。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** グラフの値(単位つき)。 */
export function formatValue(value: number | null | undefined, unit: string): string {
  if (value == null || Number.isNaN(value)) return "—";
  switch (unit) {
    case "percent":
      return `${value.toFixed(1)}%`;
    case "bytes":
      return formatBytes(value);
    case "mb":
      return value >= 1024 ? `${(value / 1024).toFixed(2)} GB` : `${value.toFixed(1)} MB`;
    case "cores":
      return `${value.toFixed(3)} コア`;
    default:
      return Number.isInteger(value) ? value.toLocaleString("ja-JP") : value.toLocaleString("ja-JP", { maximumFractionDigits: 2 });
  }
}

function formatBytes(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** 上端の値を、きりのよい数に切り上げる(値がすべて 0 のときは 1)。 */
export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) if (value <= step * exp) return step * exp;
  return 10 * exp;
}
