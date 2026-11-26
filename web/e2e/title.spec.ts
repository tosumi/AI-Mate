// 入口のタイトル画面(/。本体仕様書 §15.1 の M11 の手順1)。

import { expect, test } from "@playwright/test";

test("タイトル画面から、チャットとデザイナに入れる。運営者の画面への入口はない", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "FureAI ペルソナ" })).toBeVisible();
  const entries = page.getByRole("navigation", { name: "入口" });
  await expect(entries.getByRole("link", { name: /チャット/ })).toHaveAttribute("href", "/chat");
  await expect(entries.getByRole("link", { name: /デザイナ/ })).toHaveAttribute("href", "/designer");
  await expect(page.locator('a[href^="/ops"]')).toHaveCount(0);
  // 仮の絵が出ている(差し替えの絵がなければ title.svg)
  await expect.poll(() => page.locator(".title-art").evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);

  await entries.getByRole("link", { name: /デザイナ/ }).click();
  await expect(page).toHaveURL(/\/designer$/);
  await page.getByRole("link", { name: "タイトルに戻る" }).click();
  await expect(page).toHaveURL(/\/$/);

  await page.getByRole("navigation", { name: "入口" }).getByRole("link", { name: /チャット/ }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await page.getByRole("link", { name: "タイトルに戻る" }).click();
  await expect(page.getByRole("navigation", { name: "入口" })).toBeVisible();
});

test("スマホの幅でも、2つの入口を押せる", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 700 });
  await page.goto("/");
  const entries = page.getByRole("navigation", { name: "入口" });
  await expect(entries.getByRole("link", { name: /チャット/ })).toBeInViewport();
  await expect(entries.getByRole("link", { name: /デザイナ/ })).toBeInViewport();
});
