// 通知の購読に使う、鍵の変換のテスト(本体仕様書 §15.1 の M8)。

import { describe, expect, it } from "vitest";
import { base64UrlToBytes } from "./push";

describe("base64UrlToBytes", () => {
  it("base64url(パディングなし、- と _)を、バイト列にする", () => {
    expect(Array.from(base64UrlToBytes("-_8"))).toEqual([0xfb, 0xff]);
    expect(Array.from(base64UrlToBytes("AQID"))).toEqual([1, 2, 3]);
  });

  it("65バイトの公開鍵(非圧縮の点)を、そのまま変換する", () => {
    const key = new Uint8Array(65).map((_, i) => (i === 0 ? 4 : i));
    const text = btoa(String.fromCharCode(...key)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    expect(Array.from(base64UrlToBytes(text))).toEqual(Array.from(key));
  });
});
