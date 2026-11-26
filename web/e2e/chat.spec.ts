// 画面の通しのテスト(E2E。本体仕様書 §15.1 の M7 の手順5)。
// サインイン → 同意 → 年齢 → 会話(逐次表示、取り消し、無言、通知、失敗)→ 評価・報告。

import { expect, test } from "@playwright/test";
import { mockApi, normalReply } from "./mockApi";

const SESSION = "11111111-1111-1111-1111-111111111111";

test("サインインから同意・年齢を経て、会話し、評価と報告ができる", async ({ page }) => {
  const recorded = await mockApi(page, () => ({ events: normalReply("やあ、元気にしてた? 今日はどんな日だった?") }));
  await page.goto("/chat");

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
  // 詳しく: 3つの観点とメモを足して送り直す(同じ応答への評価は上書き)
  await page.getByRole("button", { name: "詳しく" }).click();
  const detail = page.getByRole("form", { name: "詳しい評価" });
  await detail.getByLabel("キャラクタらしさ").selectOption("5");
  await detail.getByLabel("自然さ").selectOption("3");
  await detail.getByLabel("メモ").fill("少し長い");
  await detail.getByRole("button", { name: "送る" }).click();
  await expect(page.getByText("詳しい評価を送りました(4)")).toBeVisible();
  // 報告: 理由とメモを選んで送る。個別に返事をしないことを伝える
  await page.getByRole("button", { name: "報告" }).click();
  const reportForm = page.getByRole("form", { name: "この応答を報告する" });
  await expect(reportForm.getByText("個別にはお返事しません", { exact: false })).toBeVisible();
  await reportForm.getByLabel("理由").selectOption("incorrect");
  await reportForm.getByLabel("メモ(任意)").fill("質問が多い");
  await reportForm.getByRole("button", { name: "報告する" }).click();
  await expect(page.getByText("報告しました。ありがとうございます。")).toBeVisible();

  expect(recorded.consents).toEqual([["terms", "privacy"], ["emotion_data", "memory"]]);
  expect(recorded.age).toEqual([true]);
  expect(recorded.turns).toEqual(["ただいま"]);
  expect(recorded.ratings).toEqual([
    { character_id: "sample_oshaberi", session_id: SESSION, turn_no: 1, overall: 4 },
    { character_id: "sample_oshaberi", session_id: SESSION, turn_no: 1, overall: 4, persona_fit: 5, naturalness: 3, note: "少し長い" },
  ]);
  expect(recorded.reports).toEqual([
    { character_id: "sample_oshaberi", session_id: SESSION, turn_no: 1, reason: "incorrect", note: "質問が多い" },
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
  await page.goto("/chat");
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
  await page.goto("/chat");
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
  await page.goto("/chat");
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

test("応答にマークアップが入っていても、文字として出て動かない(M11 の手順2)", async ({ page }) => {
  // サーバーは無害な形にして返すが、もし素のまま届いても、画面は動かさない(二重の守り)
  const raw = '<img src=x onerror="window.__pwned=1"> [ここ](javascript:window.__pwned=2) <script>window.__pwned=3</script>';
  await mockApi(page, () => ({ events: normalReply(raw) }), { consented: true });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByLabel("メッセージ").fill("こんにちは");
  await page.getByRole("button", { name: "送信" }).click();
  const bubble = page.locator(".bubble.character p").last();
  await expect(bubble).toContainText("<img src=x onerror=");
  await expect(bubble).toContainText("javascript:");
  await expect(page.locator(".chat-log img, .chat-log script, .chat-log a[href^='javascript']")).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
});

test("サインインを確かめられない(503)ときは、理由と「もう一度試す」を出す(M11 の手順2: 障害時の動作)", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("こんにちは") }), { consented: true });
  let down = true;
  await page.route("**/v1/account/consents", async (route) => {
    if (!down || route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      status: 503, contentType: "application/json",
      body: JSON.stringify({ detail: "いまサインインを確かめられません。少し待って、もう一度お試しください。" }),
      headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "authorization,content-type" },
    });
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByRole("alert")).toHaveText("いまサインインを確かめられません。少し待って、もう一度お試しください。");
  // 復旧したら、「もう一度試す」で会話の画面に入れる
  down = false;
  await page.getByRole("button", { name: "もう一度試す" }).click();
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByLabel("メッセージ")).toBeVisible();
});

test("応答の途中で接続が切れても、理由を出し、もう一度送れる(M11 の手順2: 障害時の動作)", async ({ page }) => {
  await mockApi(page, () => ({ events: normalReply("届いた応答です。") }), { consented: true });
  let cut = true;
  await page.route("**/v1/turns/stream", async (route) => {
    if (!cut || route.request().method() !== "POST") return route.fallback();
    return route.abort("connectionreset");
  });
  await page.goto("/chat");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByLabel("メッセージ").fill("こんにちは");
  await page.getByRole("button", { name: "送信" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  cut = false;
  await page.getByLabel("メッセージ").fill("もう一度");
  await page.getByRole("button", { name: "送信" }).click();
  await expect(page.locator(".bubble.character p").last()).toHaveText("届いた応答です。");
});
