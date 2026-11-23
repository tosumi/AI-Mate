// サインインを始められなかったときの文面のテスト(リダイレクト方式。2026-11-21)。

import { describe, expect, it } from "vitest";
import { signInErrorMessage } from "./auth";

describe("signInErrorMessage", () => {
  it("操作が進行中(2回押した、前のサインインが残っている)なら、待ってやり直す案内にする", () => {
    expect(signInErrorMessage({ errorCode: "interaction_in_progress" })).toContain("少し待ってから、もう一度お試しください");
    expect(signInErrorMessage({ errorCode: "interaction_in_progress" })).toContain("タブを閉じて");
  });

  it("ほかのエラーは、内部の文面を出さずに、一般的な案内にする", () => {
    const text = signInErrorMessage(new Error("in_mem_redirect_unavailable: internal details"));
    expect(text).toBe("サインインを始められませんでした。少し待ってから、もう一度お試しください。");
    expect(signInErrorMessage(null)).toBe(text);
  });
});
