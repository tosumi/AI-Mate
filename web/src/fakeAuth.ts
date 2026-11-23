// 画面の通しのテスト(E2E)専用の、偽物のサインイン(本体仕様書 §15.1 の M7 の手順5)。
// Vite の e2e モードのビルドでだけ使う。配信する画面(production)には入らないことを、テストで確かめる(目印: E2E-ONLY-FAKE-AUTH)。

export const FAKE_AUTH_MARKER = "E2E-ONLY-FAKE-AUTH";

export class FakeAuth {
  private signedIn = false;

  start(): Promise<boolean> {
    return Promise.resolve(this.signedIn);
  }

  async signIn(): Promise<boolean> {
    this.signedIn = true;
    return true; // 画面を移らずに、その場でサインインが終わる
  }

  async signOut(): Promise<void> {
    this.signedIn = false;
  }

  async token(): Promise<string> {
    return FAKE_AUTH_MARKER;
  }
}
