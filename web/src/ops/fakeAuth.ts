// 画面の通しのテスト(E2E)専用の、運営者の偽物のサインイン。Vite の e2e モードのビルドでだけ使う。
// 目印に E2E-ONLY-FAKE-AUTH を含める(配信用のビルドに入っていないことを、scripts/check-dist.mjs で確かめる)。

export const FAKE_OPS_TOKEN = "E2E-ONLY-FAKE-AUTH-OPS";

export class FakeOpsAuth {
  private signedIn = false;
  signOuts = 0;

  start(): Promise<boolean> {
    return Promise.resolve(this.signedIn);
  }

  async signIn(): Promise<boolean> {
    this.signedIn = true;
    return true;
  }

  async signOut(): Promise<void> {
    this.signedIn = false;
    this.signOuts += 1;
  }

  async token(): Promise<string> {
    return FAKE_OPS_TOKEN;
  }
}
