// 記憶・設定・アカウントの画面の通しのテスト(本体仕様書 §15.1 の M7 の手順3・5)。

import { expect, test } from "@playwright/test";
import { mockApi, normalReply } from "./mockApi";

test.beforeEach(async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept());
});

test("記憶とトピックを、話題に出さない・削除できる", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "記憶" }).click();

  await expect(page.getByText("ユーザーはトモと呼ばれたい")).toBeVisible();
  await expect(page.getByLabel("大切なこと")).toBeVisible();
  const cat = page.locator("li", { hasText: "ユーザーは猫を飼い始めた" });
  await cat.getByRole("button", { name: "話題に出さない" }).click();
  await expect(cat.getByRole("button", { name: "話題に出してよい" })).toBeVisible();
  await cat.getByRole("button", { name: "削除" }).click();
  await expect(page.getByText("ユーザーは猫を飼い始めた")).toHaveCount(0);

  const topic = page.locator("li", { hasText: "飼い始めた猫" });
  await topic.getByRole("button", { name: "話題に出さない" }).click();
  await expect(topic.getByText("話題に出さない", { exact: true }).first()).toBeVisible();
  await topic.getByRole("button", { name: "削除" }).click();
  await expect(page.getByText("飼い始めた猫")).toHaveCount(0);

  expect(recorded.calls).toEqual(expect.arrayContaining([
    "POST /v1/memory/m2/mute?character_id=sample_oshaberi",
    "DELETE /v1/memory/m2?character_id=sample_oshaberi",
    "POST /v1/topics/e-tama/mute?character_id=sample_oshaberi",
    "DELETE /v1/topics/e-tama?character_id=sample_oshaberi",
  ]));
});

test("設定を変えられる", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "設定" }).click();
  await page.getByLabel(/黙ったり、相づちだけで返したりしてよい/).uncheck();
  await expect(page.getByText("保存しました。")).toBeVisible();
  await page.getByLabel("黙ったときの表示").selectOption("none");
  await page.getByLabel(/つながりのある話題へ/).uncheck();
  // キャラクタから話しかけること(既定オフ)
  await expect(page.getByLabel(/キャラクタから話しかけてよい/)).not.toBeChecked();
  await page.getByLabel(/キャラクタから話しかけてよい/).check();
  await expect(page.getByText("夜21時から朝9時までは話しかけません")).toBeVisible();
  // 通知を使えないとき(サーバーに鍵がない)は、その旨だけを表示する
  await expect(page.getByText("通知はいまは使えません")).toBeVisible();
  await expect(page.getByRole("button", { name: /通知を受け取る/ })).toHaveCount(0);
  expect(recorded.settings).toEqual([
    { silence_enabled: false }, { silence_render: "none" }, { association_enabled: false }, { proactive_enabled: true },
  ]);
});

test("通知を使えるときは、この端末で受け取るボタンを出す(会話の内容は通知に出さないと説明する)", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true, push: true });
  // 自動のブラウザ(画面なし)は、通知を常に拒否する。ふつうのブラウザ(まだ決めていない)の状態にして、画面の表示だけを確かめる
  await page.addInitScript(() => Object.defineProperty(Notification, "permission", { get: () => "default" }));
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "設定" }).click();
  await expect(page.getByRole("status").filter({ hasText: "現在の設定：" })).toHaveText("現在の設定：この端末では通知を受け取りません");
  await expect(page.getByRole("button", { name: "この端末で通知を受け取る" })).toBeVisible();
  await expect(page.getByText("通知には、会話の内容は表示しません")).toBeVisible();
});

test("書き出し・同意の取り消し・アカウントの削除ができる", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "アカウント" }).click();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "書き出す" }).click();
  expect((await download).suggestedFilename()).toMatch(/^mycompanion-export-\d{4}-\d{2}-\d{2}\.json$/);

  await page.locator("li", { hasText: "記憶の保存に同意します" }).getByRole("button", { name: "取り消す" }).click();
  await expect(page.getByText("取り消しました。")).toBeVisible();
  expect(recorded.revoked).toEqual(["memory"]);

  // 削除: delete と入力するまで押せない
  const remove = page.getByRole("button", { name: "アカウントを削除する" });
  await expect(remove).toBeDisabled();
  await page.getByLabel("確認の入力").fill("delet");
  await expect(remove).toBeDisabled();
  await page.getByLabel("確認の入力").fill("delete");
  await remove.click();
  await expect(page.getByRole("heading", { name: "アカウントを削除しました" })).toBeVisible();
  await expect(page.getByText("数時間以内に削除されます")).toBeVisible();
  expect(recorded.deleted).toBe(1);
});
