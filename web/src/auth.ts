// サインイン(Entra External ID。OIDC + 認可コード + PKCE。本体仕様書 §13.4、§15.1 の M7)。
// トークンは、タブの一時保存領域(sessionStorage)に置く(2026-11-22 に変更。ユーザーの指示: 再表示でサインインの画面に戻さない)。
//   以前はメモリだけに置き、再表示のときは見えない枠で取り直していたが、iPhone などは他サイトの Cookie を使わせないので、
//   取り直せずにサインインの画面に戻っていた。sessionStorage はタブを閉じると消え、ほかのタブ・ほかのサイトからは読めない。
//   localStorage(閉じても残る)と Cookie(サーバーに送られる)には置かない。
// サインインは「リダイレクト」方式(同じタブで、サインインの画面に移って戻る。2026-11-21 に変更)。
//   以前の「ポップアップ」方式は、スマートフォンでは別のタブになり、メールのアプリへ切り替えて戻ると、
//   サインインのタブが再読み込みされて失敗した(AADSTS900561)。
//   サインインの途中の一時的な値(state、PKCE の検証用の値など)だけを、タブの一時保存領域(sessionStorage)に置く。
//   トークンは置かない。一時的な値は、サインインが終わると消え、タブを閉じても消える。
// 再読み込みのときは、Entra のサインインの状態から、画面を出さずに取り直す(取れなければ、サインインの画面へ)。

import {
  type AccountInfo,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from "@azure/msal-browser";
import type { AppConfig } from "./config";
import { FakeAuth } from "./fakeAuth";

/** サインインのやり直しが要る(画面から、利用者の操作で、もう一度サインインする)。 */
export class SignInRequired extends Error {}

export class Auth {
  private readonly msal: PublicClientApplication;
  private readonly scopes: string[];
  private account: AccountInfo | null = null;
  private started: Promise<boolean> | null = null;

  constructor(config: AppConfig) {
    this.msal = new PublicClientApplication({
      auth: {
        clientId: config.clientId,
        authority: `${config.authority.replace(/\/$/, "")}/${config.tenantId}`,
        knownAuthorities: [new URL(config.authority).host],
        redirectUri: window.location.origin,
      },
      // トークンも、サインインの途中の一時的な値も、タブの一時保存領域に置く(Cookie は使わない)
      cache: { cacheLocation: "sessionStorage", temporaryCacheLocation: "sessionStorage" },
    });
    this.scopes = [config.apiScope];
  }

  /** 起動時: サインイン済みかを返す(何度呼んでも、処理は1回)。 */
  start(): Promise<boolean> {
    this.started ??= this.begin();
    return this.started;
  }

  private async begin(): Promise<boolean> {
    await this.msal.initialize();
    // サインインの画面から戻ってきたとき: 結果を受け取る(URL の認可コードは、MSAL が消す)
    const returned = await this.msal.handleRedirectPromise().catch(() => null);
    if (returned?.account) {
      this.account = returned.account;
      return true;
    }
    // 再表示: タブに残っているサインインを使う(トークンの期限が切れていれば、更新トークンで更新する)
    const [cached] = this.msal.getAllAccounts();
    if (cached) {
      try {
        await this.msal.acquireTokenSilent({ scopes: this.scopes, account: cached });
        this.account = cached;
        return true;
      } catch {
        // 更新できない(期限切れなど): 次の方法で取り直す
      }
    }
    try {
      // メモリにトークンがない(再読み込み)ときは、Entra のサインインの状態から、画面を出さずに取り直す
      const silent = await this.msal.ssoSilent({ scopes: this.scopes });
      this.account = silent.account;
      return true;
    } catch {
      return false;
    }
  }

  /** サインイン(リダイレクト)。サインインの画面へ移る(戻ってきたら、start が結果を受け取る)。
   * その場でサインインが終わったら true(本物は、画面を移るので常に false)。 */
  async signIn(): Promise<boolean> {
    await this.msal.loginRedirect({ scopes: this.scopes });
    return false;
  }

  async signOut(): Promise<void> {
    const account = this.account ?? undefined;
    this.account = null;
    await this.msal.logoutRedirect({ account, postLogoutRedirectUri: window.location.origin });
  }

  /** API を呼ぶためのアクセストークン(期限が近ければ、更新する)。 */
  async token(): Promise<string> {
    if (!this.account) throw new SignInRequired("サインインしてください。");
    try {
      const result = await this.msal.acquireTokenSilent({ scopes: this.scopes, account: this.account });
      return result.accessToken;
    } catch (error) {
      if (error instanceof InteractionRequiredAuthError) {
        // 利用者の操作なしに、サインインの画面へ移らない。画面に、サインインのやり直しを求める
        this.account = null;
        throw new SignInRequired("サインインの有効期限が切れました。もう一度サインインしてください。");
      }
      throw error;
    }
  }
}

/** この窓が、画面を出さない取り直し(見えない枠)の戻り先か(その場合、アプリを動かさない)。 */
export function isAuthFrame(): boolean {
  const inFrame = window.parent !== window;
  return inFrame && /[?#&](code|error|state)=/.test(window.location.hash + window.location.search);
}

/** サインインを始められなかったときの、利用者向けの文面。 */
export function signInErrorMessage(error: unknown): string {
  const code = (error as { errorCode?: string } | null)?.errorCode ?? "";
  if (code === "interaction_in_progress") {
    return "サインインの準備中です。少し待ってから、もう一度お試しください。うまくいかないときは、このタブを閉じて、開き直してください。";
  }
  return "サインインを始められませんでした。少し待ってから、もう一度お試しください。";
}

/** 画面が使うサインインの形(本物と、E2E 用の偽物で同じ)。 */
export type AuthLike = Pick<Auth, "start" | "signIn" | "signOut" | "token">;

/** サインインを作る。E2E のモードのビルドだけ、偽物にする(production のビルドでは、この分岐ごと消える)。 */
export function createAuth(config: AppConfig): AuthLike {
  if (import.meta.env.MODE === "e2e") return new FakeAuth();
  return new Auth(config);
}
