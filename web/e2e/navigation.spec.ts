// ページの切り替え・再表示で、会話と見ていたページが残ることの通しのテスト(2026-11-22 のユーザーの指示)。

import { expect, test } from "@playwright/test";
import { mockApi, normalReply } from "./mockApi";

test("ページやキャラクタを切り替えても、会話が残る", async ({ page }) => {
  await mockApi(page, (text) => ({ events: normalReply(`「${text}」の返事`) }), { consented: true });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  const chat = page.getByRole("region", { name: "会話" });
  await chat.getByLabel("メッセージ").fill("こんにちは");
  await chat.getByRole("button", { name: "送信" }).click();
  await expect(chat.getByText("「こんにちは」の返事")).toBeVisible();
  // 入力途中の文字も残す
  await chat.getByLabel("メッセージ").fill("書きかけ");

  await page.getByRole("button", { name: "設定" }).click();
  await expect(page.getByRole("region", { name: "設定" })).toBeVisible();
  await page.getByRole("button", { name: "会話" }).click();
  await expect(chat.getByText("「こんにちは」の返事")).toBeVisible();
  await expect(chat.getByLabel("メッセージ")).toHaveValue("書きかけ");

  // キャラクタを切り替えて戻っても、それぞれの会話が残る
  await chat.getByLabel("キャラクタ").selectOption("sample_kamoku");
  await expect(chat.getByText("「こんにちは」の返事")).toHaveCount(0);
  await chat.getByLabel("キャラクタ").selectOption("sample_oshaberi");
  await expect(chat.getByText("「こんにちは」の返事")).toBeVisible();
});

test("記憶・設定は、会話の画面が出たあと、裏で先に読み込んでおく", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ") }), { consented: true });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect.poll(() => recorded.calls.filter((c) => c.startsWith("GET /v1/memory") || c === "GET /v1/settings").length).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: "記憶" }).click();
  await expect(page.getByText("ユーザーはトモと呼ばれたい")).toBeVisible(); // 「読み込んでいます」を待たない
});

test("再表示すると、見ていたページとキャラクタに戻り、いまの会話の続きを読み直す", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("やあ") }), {
    consented: true,
    conversation: {
      sample_kamoku: {
        session_id: "44444444-4444-4444-4444-444444444444",
        turns: [
          { turn_no: 1, user_input: "今日は雨だね", reply_text: "傘、持っていった?", response_mode: "full" },
          { turn_no: 2, user_input: "うん", reply_text: null, response_mode: "silence" },
        ],
      },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  const chat = page.getByRole("region", { name: "会話" });
  await chat.getByLabel("キャラクタ").selectOption("sample_kamoku");
  await expect(chat.getByText("傘、持っていった?")).toBeVisible();
  await page.getByRole("button", { name: "設定" }).click();

  // 再表示(E2E の偽物のサインインは、メモリだけなので、もう一度サインインの操作をする)
  await page.reload();
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByRole("region", { name: "設定" })).toBeVisible(); // 見ていたページ
  await page.getByRole("button", { name: "会話" }).click();
  await expect(chat.getByLabel("キャラクタ")).toHaveValue("sample_kamoku"); // 開いていたキャラクタ
  await expect(chat.getByText("今日は雨だね")).toBeVisible(); // 会話の続き(サーバーから)
  await expect(chat.getByText("傘、持っていった?")).toBeVisible();
  // 読み直した応答にも、評価・報告ができる(ターン番号つき)
  await expect(chat.getByRole("button", { name: "この応答を 4 で評価する" }).first()).toBeVisible();
});

test("ページの見出しは画面に出さず、キャラクタの選択の行は、スクロールしてもメニューのすぐ下に残る", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 600 });
  await mockApi(page, () => ({ events: normalReply("長めの返事です。".repeat(15)) }), { consented: true });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();

  // 会話: 何往復かして、ページをスクロールさせる
  const chat = page.getByRole("region", { name: "会話" });
  for (let i = 1; i <= 3; i += 1) {
    await chat.getByRole("textbox", { name: "メッセージ" }).fill(`${i}回目`);
    await chat.getByRole("button", { name: "送信" }).click();
    await expect(chat.locator(".bubble.character")).toHaveCount(i);
  }
  const stuck = async (bar: import("@playwright/test").Locator) => {
    const header = await page.locator(".app-header").boundingBox();
    const box = await bar.boundingBox();
    return header && box ? Math.abs(box.y - (header.y + header.height)) <= 1 : false;
  };
  await page.mouse.wheel(0, 2000);
  await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0);
  await expect.poll(() => stuck(chat.locator(".page-bar"))).toBe(true);
  await expect(chat.getByLabel("キャラクタ")).toBeInViewport();

  // 設定・記憶・アカウント: 見出しは画面に出ない(読み上げ用には残る)
  for (const name of ["設定", "記憶", "アカウント"]) {
    await page.getByRole("navigation", { name: "画面" }).getByRole("button", { name }).click();
    const region = page.getByRole("region", { name });
    await expect(region).toBeVisible();
    await expect(region.getByRole("heading", { name, level: 1 })).not.toBeInViewport();
  }
  // 記憶: キャラクタの選択の行から始まり、スクロールしても残る
  await page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: "記憶" }).click();
  const memories = page.getByRole("region", { name: "記憶" });
  await expect(memories.getByLabel("キャラクタ")).toBeInViewport();
});

test("ログインした最初の画面に、終わった会話も含めて、これまでの会話を表示する", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("また話せてうれしい") }), {
    consented: true,
    conversation: {
      // 無入力で終わった会話(進行中ではないので session_id は null。各ターンにセッションが付く)
      sample_oshaberi: {
        session_id: null,
        turns: [
          { session_id: "66666666-6666-6666-6666-666666666666", turn_no: 1, user_input: "豚丼に名前を付けたよ", reply_text: "しあわせの豚丼、いい名前!", response_mode: "full" },
        ],
      },
    },
  });
  await page.goto("/");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  const chat = page.getByRole("region", { name: "会話" });
  // 何も操作しなくても、最初の画面に出る
  await expect(chat.getByText("豚丼に名前を付けたよ")).toBeVisible();
  await expect(chat.getByText("しあわせの豚丼、いい名前!")).toBeVisible();
  // 続けて話すと、これまでの会話のあとに続く
  await chat.getByRole("textbox", { name: "メッセージ" }).fill("ありがとう");
  await chat.getByRole("button", { name: "送信" }).click();
  await expect(chat.getByText("また話せてうれしい")).toBeVisible();
  await expect(chat.locator(".bubble.user")).toHaveText([/豚丼に名前を付けたよ/, /ありがとう/]);
});

test("読み込みが終わるまで、各ページに「読み込んでいます…」を出す(空白のページにしない)", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("やあ") }), {
    consented: true,
    conversation: {
      sample_oshaberi: { session_id: null, turns: [{ session_id: "77777777-7777-7777-7777-777777777777", turn_no: 1, user_input: "前の話", reply_text: "覚えてるよ", response_mode: "full" }] },
    },
  });
  // これまでの会話・記憶・同意の読み込みを、合図があるまで止める
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  for (const pattern of ["**/v1/conversation**", "**/v1/memory**", "**/v1/account/consents"]) {
    await page.route(pattern, async (route) => {
      await gate;
      await route.fallback();
    });
  }
  await page.goto("/");
  // 同意の確認も止まるので、サインインの前に止めるのは、会話・記憶だけにする
  await page.unroute("**/v1/account/consents");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  const chat = page.getByRole("region", { name: "会話" });
  await expect(chat.getByText("読み込んでいます…")).toBeVisible();
  await page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: "記憶" }).click();
  await expect(page.getByRole("region", { name: "記憶" }).getByText("読み込んでいます…").first()).toBeVisible();

  release();
  await expect(page.getByRole("region", { name: "記憶" }).getByText("読み込んでいます…")).toHaveCount(0);
  await page.getByRole("navigation", { name: "画面" }).getByRole("button", { name: "会話" }).click();
  await expect(chat.getByText("覚えてるよ")).toBeVisible();
  await expect(chat.getByText("読み込んでいます…")).toHaveCount(0);
});
