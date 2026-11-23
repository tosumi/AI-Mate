// 接続先の設定。ビルドのときに、環境変数(VITE_*)から入れる。
// どれも秘密情報ではないが、アカウントの情報なので、リポジトリには書かない(web/.env.local。Git の対象外)。

export type AppConfig = {
  apiUrl: string; // API の URL(例: https://<api>.azurecontainerapps.io)
  authority: string; // サインインの URL(例: https://<サブドメイン>.ciamlogin.com/)
  tenantId: string; // 外部テナントの ID
  clientId: string; // Web 用のアプリの登録(SPA)の ID
  apiScope: string; // API の利用範囲(例: api://<API のクライアント ID>/companion.access)
};

export function loadConfig(env: Record<string, string | undefined> = import.meta.env): AppConfig {
  const config = {
    apiUrl: (env.VITE_API_URL ?? "").replace(/\/$/, ""),
    authority: env.VITE_ENTRA_AUTHORITY ?? "",
    tenantId: env.VITE_ENTRA_TENANT_ID ?? "",
    clientId: env.VITE_ENTRA_CLIENT_ID ?? "",
    apiScope: env.VITE_API_SCOPE ?? "",
  };
  const missing = Object.entries(config)
    .filter(([, value]) => !value)
    .map(([key]) => key);
  if (missing.length > 0) {
    throw new Error(`接続先の設定が足りません: ${missing.join(", ")}`);
  }
  return config;
}
