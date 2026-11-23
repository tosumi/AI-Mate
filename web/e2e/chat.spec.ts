// 画面の通しのテスト(E2E。本体仕様書 §15.1 の M7 の手順5)。
// サインイン → 同意 → 年齢 → 会話(逐次表示、取り消し、無言、通知、失敗)→ 評価・報告。

import { expect, test } from "@playwright/test";
import { mockApi, normalReply } from "./mockApi";

const SESSION = "11111111-1111-1111-1111-111111111111";

test("サインインから同意・年齢を経て、会話し、評価と報告ができる", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ、元気にしてた? 今日はどんな日だった?") }));
  await page.goto("/");

  await page.getByRole("button", { name: "メールでサインイン" }).click();

  // 同意: 必須の項目にチェックするまで、次へ進めない(あらかじめチェックを入れない)
  await expect(page.getByRole("heading", { name: "はじめに" })).toBeVisible();
  const next = page.getByRole("button", { name: "次へ" });
  await expect(next).toBeDisabled();
  await page.getByLabel(/利用規約に同意します/).check();
  await expect(next).toBeDisabled();
  await page.getByLabel(/プライバシーポリシーを確認しました/).check();
  await next.click();
  await expect(page.getByRole("heading", { name: "感情の推定と記憶" })).toBeVisible();
  await page.getByLabel(/感情の推定に同意します/).check();
  await page.getByLabel(/記憶の保存に同意します/).check();
  await page.getByRole("button", { name: "次へ" }).click();

  // 年齢
  await expect(page.getByRole("heading", { name: "年齢の確認" })).toBeVisible();
  await page.getByRole("button", { name: "はい" }).click();

  // 任意の設定(何も選ばずに進む)
  await expect(page.getByRole("heading", { name: "任意の設定" })).toBeVisible();
  await page.getByRole("button", { name: "次へ" }).click();

  // 会話
  await expect(page.getByLabel("キャラクタ")).toHaveValue("sample_oshaberi");
  await page.getByLabel("メッセージ").fill("ただいま");
  await page.getByRole("button", { name: "送信" }).click();
  await expect(page.locator(".bubble.user")).toContainText("ただいま");
  await expect(page.locator(".read")).toBeVisible();
  await expect(page.locator(".bubble.character p")).toHaveText("やあ、元気にしてた? 今日はどんな日だった?");

  // 評価と報告(対象のターンの番号つき。会話の本文は送らない)
  await page.getByRole("button", { name: "この応答を 4 で評価する" }).click();
  await expect(page.getByText("評価しました(4)")).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept("質問が多い"));
  await page.getByRole("button", { name: "報告" }).click();
  await expect(page.getByText("報告しました。ありがとうございます。")).toBeVisible();

  expect(recorded.consents).toEqual([["terms", "privacy"], ["emotion_data", "memory"]]);
  expect(recorded.age).toEqual([true]);
  expect(recorded.turns).toEqual(["ただいま"]);
  expect(recorded.ratings).toEqual([{ character_id: "sample_oshaberi", session_id: SESSION, turn_no: 1, overall: 4 }]);
  expect(recorded.reports).toEqual([
    { character_id: "sample_oshaberi", session_id: SESSION, turn_no: 1, reason: "inappropriate", note: "質問が多い" },
  ]);
});

test("確認に引っかかった文字は、取り消して最終の応答に置き換える", async ({ page }) => {
  await mockApi(
    page,
    () => ({
      events: [
        ["accepted", {}],
        ["typing", {}],
        ["delta", { text: "ねえ、" }],
        ["delta", { text: "危ない文" }],
        ["retract", { reason: "check" }],
        ["message", { type: "message", text: "安全な応答です。", variant: "filtered" }],
        ["done", { session_id: SESSION, turn_no: 1 }],
      ],
    }),
    { consented: true },
  );
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByLabel("メッセージ").fill("好き?");
  await page.getByRole("button", { name: "送信" }).click();
  await expect(page.locator(".bubble.character p")).toHaveText("安全な応答です。");
  await expect(page.locator(".chat-log")).not.toContainText("危ない文");
});

test("無言・相談窓口の案内・失敗・送りすぎを表示する", async ({ page }) => {
  const scripts: Record<string, () => { status?: number; events: [string, unknown][] }> = {
    "…": () => ({ events: [["accepted", {}], ["silence", { type: "silence", render: "dots" }], ["done", { session_id: SESSION, turn_no: 1 }]] }),
    "つらい": () => ({
      events: [
        ...normalReply("話してくれてありがとう。", 2).slice(0, -1),
        ["notice", { type: "system_notice", kind: "crisis_resources", text: "つらい気持ちを、専門の相談員に話せる窓口があります。" }],
        ["done", { session_id: SESSION, turn_no: 2 }],
      ],
    }),
    "失敗": () => ({ events: [["accepted", {}], ["typing", {}], ["error", { code: "llm_unavailable", message: "いま応答を作れませんでした。", status: 503 }]] }),
    "連打": () => ({ status: 429, events: [] }),
  };
  await mockApi(page, (text) => scripts[text](), { consented: true });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();

  const send = async (text: string) => {
    await page.getByLabel("メッセージ").fill(text);
    await page.getByRole("button", { name: "送信" }).click();
  };
  await send("…");
  await expect(page.getByLabel("(無言)")).toBeVisible();
  await send("つらい");
  await expect(page.getByRole("note")).toContainText("相談員に話せる窓口");
  await send("失敗");
  await expect(page.getByRole("alert")).toContainText("いま応答を作れませんでした。");
  await send("連打");
  await expect(page.getByRole("alert").last()).toContainText("少し間をおいてから");
});

test("スマートフォンの画面で、会話が長くなっても、最後の吹き出しが入力欄の裏に隠れない", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 });
  const long = "長めの返事です。".repeat(12);
  await mockApi(page, () => ({ events: normalReply(long) }), { consented: true });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  const input = page.getByRole("textbox", { name: "メッセージ" });
  for (let i = 1; i <= 4; i += 1) {
    await input.fill(`${i}回目の発言`);
    await page.getByRole("button", { name: "送信" }).click();
    await expect(page.locator(".bubble.character")).toHaveCount(i);
    // 最後の吹き出しの下端が、入力欄の上端より上にある
    await expect
      .poll(async () => {
        const last = await page.locator(".bubble.character").last().boundingBox();
        const form = await page.locator(".chat-input").boundingBox();
        return last && form ? last.y + last.height <= form.y + 1 : false;
      })
      .toBe(true);
  }
});
