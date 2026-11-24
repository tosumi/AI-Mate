// 運営者の画面(/ops。本体仕様書 §15.1 の M9)の接続先。ビルドのときに、環境変数(VITE_*)から入れる。
// 運営者は、一般ユーザー(外部テナント)とは別の、今のテナントでサインインする。どれも秘密情報ではない。

export type OpsConfig = {
  apiUrl: string; // API の URL(一般ユーザーと同じ API。経路は /ops/v1)
  tenantId: string; // 運営者のテナントの ID
  clientId: string; // 運営者の画面のアプリの登録(SPA)の ID
  apiScope: string; // 運営者の API の利用範囲(例: api://<運営者の API の ID>/ops.access)
};

export function loadOpsConfig(env: Record<string, string | undefined> = import.meta.env): OpsConfig | null {
  const config = {
    apiUrl: (env.VITE_API_URL ?? "").replace(/\/$/, ""),
    tenantId: env.VITE_OPS_TENANT_ID ?? "",
    clientId: env.VITE_OPS_CLIENT_ID ?? "",
    apiScope: env.VITE_OPS_API_SCOPE ?? "",
  };
  // 1つでも欠けていれば、運営者の画面は使えない(設定していない環境)
  return Object.values(config).every(Boolean) ? config : null;
}
