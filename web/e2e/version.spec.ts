// 新しい版の表示(本体仕様書 §15.1 の M10 の手順1)。配信のあと、開いたままの画面に「新しい版があります」を出す。

import { expect, test } from "@playwright/test";

test("画面に戻ったとき、配信されている版が違えば、新しい版があると出す", async ({ page }) => {
  let version: string | null = null;
  await page.route("**/version.json", (route) =>
    version ? route.fulfill({ contentType: "application/json", body: JSON.stringify({ version }) }) : route.fulfill({ status: 404 }),
  );
  await page.goto("/chat");
  await expect(page.getByRole("button", { name: "メールでサインイン" })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("新しい版があります。")).toHaveCount(0);

  version = "a-newer-version";
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByText("新しい版があります。")).toBeVisible();
  await page.getByRole("button", { name: "読み込み直す" }).click();
  await expect(page.getByRole("button", { name: "メールでサインイン" })).toBeVisible();
});
