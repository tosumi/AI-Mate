import { describe, expect, it } from "vitest";
import { APP_VERSION, isOutdated } from "./version";

const reply = (body: unknown, ok = true) => (async () => ({ ok, json: async () => body })) as unknown as typeof fetch;

describe("新しい版の表示", () => {
  it("配信されている版が違うときだけ、古いとみなす", async () => {
    expect(await isOutdated(reply({ version: APP_VERSION }))).toBe(false);
    expect(await isOutdated(reply({ version: "other-version" }))).toBe(true);
  });

  it("読めないときは、古いとみなさない(開発用のサーバー・通信の失敗)", async () => {
    expect(await isOutdated(reply({}, false))).toBe(false);
    expect(await isOutdated(reply("<html>"))).toBe(false);
    expect(await isOutdated((async () => { throw new Error("offline"); }) as unknown as typeof fetch)).toBe(false);
  });
});
