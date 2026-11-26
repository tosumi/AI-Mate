// キャラクタから話しかけたメッセージの表示の通しのテスト(本体仕様書 §15.1 の M8 の手順2)。

import { expect, test } from "@playwright/test";
import { mockApi, normalReply } from "./mockApi";

const MESSAGE = {
  character_id: "sample_kamoku",
  name: "シズク",
  session_id: "22222222-2222-2222-2222-222222222222",
  turn_no: 1,
  text: "そういえば、面接どうだった?",
  ts: "2026-11-21T03:00:00Z",
};

test("話しかけてきたキャラクタを先に開き、メッセージを表示して既読にする。返事は同じ会話の続き", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("よかったね。", 2) }), {
    consented: true,
    proactive: [MESSAGE],
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();

  // 話しかけてきたキャラクタ(一覧の2番目)が選ばれ、メッセージが会話の最初に出る
  await expect(page.getByLabel("キャラクタ")).toHaveValue("sample_kamoku");
  const bubble = page.locator(".bubble.character").first();
  await expect(bubble).toContainText("そういえば、面接どうだった?");
  await expect(bubble).toContainText("から話しかけてきました");
  await expect.poll(() => recorded.seen).toEqual([{ character_id: "sample_kamoku", session_id: MESSAGE.session_id }]);

  // このメッセージにも、評価・報告ができる(ターン番号つき)
  await bubble.getByRole("button", { name: "この応答を 4 で評価する" }).click();
  await expect.poll(() => recorded.ratings).toEqual([
    { character_id: "sample_kamoku", session_id: MESSAGE.session_id, turn_no: 1, overall: 4 },
  ]);

  // 返事を送る(サーバー側で、同じセッションの2ターン目になる)
  await page.getByLabel("メッセージ").fill("うまくいったよ!");
  await page.getByRole("button", { name: "送信" }).click();
  await expect(page.getByText("よかったね。")).toBeVisible();
  expect(recorded.turns).toEqual(["うまくいったよ!"]);

  // ほかのキャラクタに切り替えて戻っても、会話は残り、同じメッセージを二度出さない
  await page.getByLabel("キャラクタ").selectOption("sample_oshaberi");
  await page.getByLabel("キャラクタ").selectOption("sample_kamoku");
  await expect(page.getByText("そういえば、面接どうだった?")).toHaveCount(1);
  await expect(page.getByText("よかったね。")).toBeVisible();
  expect(recorded.seen).toHaveLength(1);
});

test("ほかのキャラクタからのメッセージには、一覧に印を付け、開いたときに表示する", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), {
    consented: true,
    proactive: [
      { ...MESSAGE, character_id: "sample_oshaberi", name: "ハル", session_id: "33333333-3333-3333-3333-333333333333", text: "キャンプの話、また聞かせて" },
      MESSAGE,
    ],
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByLabel("キャラクタ")).toHaveValue("sample_oshaberi");
  await expect(page.getByText("キャンプの話、また聞かせて")).toHaveCount(1);
  // まだ開いていないシズクには、印を付ける
  await expect(page.getByRole("option", { name: "シズク(新しいメッセージ)" })).toHaveCount(1);
  await page.getByLabel("キャラクタ").selectOption("sample_kamoku");
  await expect(page.getByText("そういえば、面接どうだった?")).toHaveCount(1);
  await expect(page.getByRole("option", { name: "シズク", exact: true })).toHaveCount(1);
  await expect.poll(() => recorded.seen.length).toBe(2);
});

test("画面を開いたまま話しかけられたら、画面に戻ったときに表示する(読み直さない)", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByLabel("キャラクタ")).toHaveValue("sample_oshaberi");
  await page.getByRole("textbox", { name: "メッセージ" }).fill("書きかけ");

  // 開いているキャラクタ(ハル)と、ほかのキャラクタ(シズク)が話しかける
  recorded.addProactive({ ...MESSAGE, character_id: "sample_oshaberi", name: "ハル", session_id: "44444444-4444-4444-4444-444444444444", text: "イルミネーション見に行けた?" });
  recorded.addProactive({ ...MESSAGE, character_id: "sample_kamoku", name: "シズク", session_id: "55555555-5555-5555-5555-555555555555", text: "キャンプどうだった?" });
  // 画面に戻る(タブを前に出す・通知を押す)
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));

  await expect(page.getByText("イルミネーション見に行けた?")).toHaveCount(1);
  await expect(page.getByRole("option", { name: "シズク(新しいメッセージ)" })).toHaveCount(1);
  await expect.poll(() => recorded.seen).toEqual([{ character_id: "sample_oshaberi", session_id: "44444444-4444-4444-4444-444444444444" }]);
  // 読み直していないので、書きかけの文字が残る
  await expect(page.getByRole("textbox", { name: "メッセージ" })).toHaveValue("書きかけ");

  // もう一度戻っても、同じメッセージを二度出さない
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.getByLabel("キャラクタ").selectOption("sample_kamoku");
  await expect(page.getByText("キャンプどうだった?")).toHaveCount(1);
  await page.getByLabel("キャラクタ").selectOption("sample_oshaberi");
  await expect(page.getByText("イルミネーション見に行けた?")).toHaveCount(1);
  await expect.poll(() => recorded.seen.length).toBe(2);
});
