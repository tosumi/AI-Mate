// 運営者のサインイン(今のテナント。本体仕様書 §15.1 の M9)。一般ユーザーのサインイン(auth.ts)とは、別の MSAL を使う。
// - トークンは、タブの一時保存領域(sessionStorage)に置く(一般ユーザーと同じ。localStorage・Cookie には置かない)。
//   同じタブで一般ユーザーの画面も使うと、同じ領域にアカウントが並ぶので、運営者のテナントのアカウントだけを使う。
// - 画面を出さない取り直し(ssoSilent)はしない。新しいタブ・サインアウトのあとは、必ずボタンからサインインする。
// - 30分操作がなければ、トークンを消して、Entra からもサインアウトする(OpsApp)。

import { type AccountInfo, InteractionRequiredAuthError, PublicClientApplication } from "@azure/msal-browser";
import { SignInRequired } from "../auth";
import type { OpsConfig } from "./config";
import { FakeOpsAuth } from "./fakeAuth";

export const OPS_PATH = "/ops";

export class OpsAuth {
  private readonly msal: PublicClientApplication;
  private readonly scopes: string[];
  private readonly tenantId: string;
  private account: AccountInfo | null = null;
  private started: Promise<boolean> | null = null;

  constructor(config: OpsConfig) {
    this.msal = new PublicClientApplication({
      auth: {
        clientId: config.clientId,
        authority: `https://login.microsoftonline.com/${config.tenantId}`,
        redirectUri: `${window.location.origin}${OPS_PATH}`,
        postLogoutRedirectUri: `${window.location.origin}${OPS_PATH}`,
      },
      cache: { cacheLocation: "sessionStorage", temporaryCacheLocation: "sessionStorage" },
    });
    this.scopes = [config.apiScope];
    this.tenantId = config.tenantId;
  }

  /** 起動時: サインイン済みかを返す(何度呼んでも、処理は1回)。 */
  start(): Promise<boolean> {
    this.started ??= this.begin();
    return this.started;
  }

  private async begin(): Promise<boolean> {
    await this.msal.initialize();
    const returned = await this.msal.handleRedirectPromise().catch(() => null);
    if (returned?.account && returned.account.tenantId === this.tenantId) {
      this.account = returned.account;
      return true;
    }
    // 再表示: このタブに残っている、運営者のテナントのサインインだけを使う
    const [cached] = this.msal.getAllAccounts({ tenantId: this.tenantId });
    if (!cached) return false;
    try {
      await this.msal.acquireTokenSilent({ scopes: this.scopes, account: cached });
      this.account = cached;
      return true;
    } catch {
      return false;
    }
  }

  async signIn(): Promise<boolean> {
    await this.msal.loginRedirect({ scopes: this.scopes, prompt: "select_account" });
    return false;
  }

  /** サインアウト。先にこのタブのトークンを消し、そのあと Entra からもサインアウトする(画面を移る)。 */
  async signOut(): Promise<void> {
    const account = this.account ?? undefined;
    this.account = null;
    await this.msal.clearCache({ account }).catch(() => undefined);
    // アカウントの選択の画面を出さずにサインアウトする(ID トークンの login_hint。アプリの登録の省略可能な要求で付ける)
    const logoutHint = account?.idTokenClaims?.login_hint;
    await this.msal.logoutRedirect({ account, logoutHint });
  }

  async token(): Promise<string> {
    if (!this.account) throw new SignInRequired("サインインしてください。");
    try {
      return (await this.msal.acquireTokenSilent({ scopes: this.scopes, account: this.account })).accessToken;
    } catch (error) {
      if (error instanceof InteractionRequiredAuthError) {
        this.account = null;
        throw new SignInRequired("サインインの有効期限が切れました。もう一度サインインしてください。");
      }
      throw error;
    }
  }
}

export type OpsAuthLike = Pick<OpsAuth, "start" | "signIn" | "signOut" | "token">;

/** E2E のモードのビルドだけ、偽物にする(production のビルドでは、この分岐ごと消える)。 */
export function createOpsAuth(config: OpsConfig): OpsAuthLike {
  if (import.meta.env.MODE === "e2e") return new FakeOpsAuth();
  return new OpsAuth(config);
}
