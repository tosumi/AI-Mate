// 運営者の画面(/ops。本体仕様書 §15.1 の M9 の手順4)の通しのテスト。API はテストの中で差し替える。

import { expect, type Page, test } from "@playwright/test";
import { API } from "./mockApi";

const ROLE_PERMISSIONS: Record<string, string[]> = {
  admin: ["view_self", "view_reports", "handle_reports", "view_ratings", "view_audit", "view_system"],
  support: ["view_self", "view_reports", "handle_reports", "view_ratings"],
  designer: ["view_self", "view_ratings"],
  auditor: ["view_self", "view_audit"],
};

const REPORT = {
  report_id: "aaaaaaaa-0000-0000-0000-000000000001", user: "u-0123456789abcdef", character_id: "sample_kamoku",
  session_id: "s1", turn_no: 3, reason: "harmful", has_note: true, status: "open",
  created_at: "2026-11-24T01:00:00+00:00", ops_updated_at: null,
};

type Recorded = { calls: string[]; patches: unknown[] };

export async function mockOps(page: Page, role: string): Promise<Recorded> {
  const recorded: Recorded = { calls: [], patches: [] };
  let report = { ...REPORT, note: "怖い言い方だった", ops_memo: null as string | null, ops_updated_by: null };
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body), headers: cors() });
  await page.route(`${API}/ops/v1/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors() });
    if (request.headers()["authorization"] !== "Bearer E2E-ONLY-FAKE-AUTH-OPS") return route.fulfill(json({ detail: "unauthorized" }, 401));
    const path = url.pathname.replace("/ops/v1", "");
    recorded.calls.push(`${request.method()} ${path}${url.search}`);
    const permissions = ROLE_PERMISSIONS[role];
    const need: Record<string, string> = { "/reports": "view_reports", "/ratings": "view_ratings", "/audit-logs": "view_audit", "/system/metrics": "view_system" };
    const required = Object.entries(need).find(([prefix]) => path.startsWith(prefix))?.[1];
    if (required && !permissions.includes(required)) return route.fulfill(json({ detail: "forbidden" }, 403));
    if (path === "/me") return route.fulfill(json({ roles: [role], permissions }));
    if (path === "/reports") return route.fulfill(json({ reports: [{ ...REPORT, status: report.status }] }));
    if (path === `/reports/${REPORT.report_id}` && request.method() === "PATCH") {
      if (!permissions.includes("handle_reports")) return route.fulfill(json({ detail: "forbidden" }, 403));
      const body = JSON.parse(request.postData() ?? "{}");
      recorded.patches.push(body);
      report = { ...report, ...(body.status ? { status: body.status } : {}), ...(body.memo !== undefined ? { ops_memo: body.memo } : {}), ops_updated_at: "2026-11-24T02:00:00+00:00" };
      return route.fulfill(json({ ...report, guardrails: null }));
    }
    if (path === `/reports/${REPORT.report_id}`) {
      return route.fulfill(json({
        ...report,
        guardrails: {
          ts: "2026-11-24T00:59:00+00:00", response_mode: "brief", filter_mode: "on", safety_flags: ["output_softened"],
          silence_reason: null, gate_rule_hits_json: ["R2"], system_notices: [], character_version: 4,
          compose_mode: "single", reply_model: "gpt-x", reply_prompt_version: "p7",
          screening: [{ layer: "output", category: "violence", severity: "medium", detector: "guardrail", score: 0.7, action: "annotate" }],
        },
      }));
    }
    if (path === "/ratings") {
      return route.fulfill(json({ since: "2026-10-25T00:00:00+00:00", min_n: 5, rows: [
        { character_id: "sample_kamoku", character_version: 4, compose_mode: "single", reply_model: "gpt-x", reply_prompt_version: "p7", response_mode: "normal", raters: 6, n: 12, overall: 4.25, persona_fit: 4, naturalness: 3.5, empathy: 4.5, low: 1 },
        { character_id: "sample_oshaberi", character_version: 2, compose_mode: "single", reply_model: "gpt-x", reply_prompt_version: "p7", response_mode: "normal", raters: 1, n: 2, overall: null, persona_fit: null, naturalness: null, empathy: null, low: null },
      ] }));
    }
    if (path === "/audit-logs") {
      return route.fulfill(json({ entries: [
        { log_id: "l1", ts: "2026-11-24T02:00:00+00:00", actor_type: "operator", actor_id: "oid-1", action: "report_updated", target_user: null, details: { decision: "allowed", roles: ["support"], report_id: REPORT.report_id, status: "resolved", memo_changed: true } },
        { log_id: "l2", ts: "2026-11-24T01:00:00+00:00", actor_type: "user", actor_id: "u-0123456789abcdef", action: "report_created", target_user: "u-0123456789abcdef", details: {} },
      ] }));
    }
    if (path === "/system/metrics") {
      const period = url.searchParams.get("period") ?? "24h";
      const end = "2026-11-24T12:00:00+00:00";
      const point = (h: number, v: number): [string, number] => [new Date(Date.parse(end) - h * 3600_000).toISOString(), v];
      return route.fulfill(json({
        period, interval: "PT1H", start: "2026-11-23T12:00:00+00:00", end, generated_at: "2026-11-24T12:34:00+00:00",
        panels: [
          { id: "api", title: "API(Container Apps)", configured: true, available: true, series: [
            { id: "replicas", label: "レプリカ数", unit: "count", points: [point(3, 1), point(2, 2), point(1, 1)] },
            { id: "cpu", label: "CPU", unit: "percent", points: [point(3, 12.5), point(2, 40.25), point(1, 8)] },
          ] },
          { id: "postgres", title: "PostgreSQL", configured: true, available: false, series: [] },
          { id: "openai", title: "Azure OpenAI", configured: false, available: false, series: [] },
        ],
      }));
    }
    return route.fulfill(json({ detail: "not found" }, 404));
  });
  return recorded;
}

function cors() {
  return {
    "access-control-allow-origin": "http://localhost:5174",
    "access-control-allow-headers": "authorization,content-type",
    "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE",
  };
}

export async function signIn(page: Page) {
  await page.goto("/ops");
  await expect(page.getByRole("heading", { name: "運営者のサインイン" })).toBeVisible();
  await page.getByRole("button", { name: "サインイン" }).click();
}

const nav = (page: Page) => page.getByRole("navigation", { name: "運営の画面" });

test("管理者は、すべてのタブを見られ、ダッシュボードでグラフを見られる", async ({ page }) => {
  const recorded = await mockOps(page, "admin");
  await signIn(page);
  await expect(page.getByText("管理者", { exact: true })).toBeVisible();
  await expect(nav(page).getByRole("button")).toHaveText(["報告", "評価", "監査ログ", "ダッシュボード"]);

  await nav(page).getByRole("button", { name: "ダッシュボード" }).click();
  await expect(page.getByRole("img", { name: "レプリカ数" })).toBeVisible();
  await expect(page.getByRole("img", { name: "CPU" })).toBeVisible();
  await expect(page.getByText("最新 8.0%")).toBeVisible();
  await expect(page.getByText("いまは読めませんでした")).toBeVisible();
  await expect(page.getByText("対象のリソースを設定していません")).toBeVisible();
  await page.getByRole("button", { name: "7日" }).click();
  await expect.poll(() => recorded.calls).toContain("GET /system/metrics?period=7d");
  // 一般ユーザーの API(/v1)は呼ばない
  expect(recorded.calls.every((c) => !c.startsWith("GET /v1"))).toBe(true);
});

test("サポートは、報告を開いて、ガードレールの記録を見て、対応を記録できる", async ({ page }) => {
  const recorded = await mockOps(page, "support");
  await signIn(page);
  await expect(nav(page).getByRole("button")).toHaveText(["報告", "評価"]);

  await page.getByRole("button", { name: /の報告を開く/ }).click();
  const detail = page.getByRole("complementary", { name: "報告の詳細" });
  await expect(detail.getByText("怖い言い方だった")).toBeVisible();
  await expect(detail.getByText("output_softened")).toBeVisible();
  await expect(detail.getByRole("cell", { name: "violence" })).toBeVisible();

  await detail.getByLabel("対応状況").selectOption("resolved");
  await detail.getByLabel(/対応メモ/).fill("表現の調整を依頼");
  await detail.getByRole("button", { name: "保存" }).click();
  await expect(detail.getByText("保存しました。")).toBeVisible();
  expect(recorded.patches).toEqual([{ status: "resolved", memo: "表現の調整を依頼" }]);
  // 一覧の状況も変わる
  await expect(page.getByRole("table").first().getByText("対応済み")).toBeVisible();

  await nav(page).getByRole("button", { name: "評価" }).click();
  const small = page.getByRole("row", { name: /sample_oshaberi/ });
  await expect(small.getByRole("cell", { name: "—" }).first()).toBeVisible();
  await expect(page.getByRole("row", { name: /sample_kamoku/ }).getByRole("cell", { name: "4.25" })).toBeVisible();
});

test("設計者は評価だけ、監査は監査ログだけを見られる", async ({ page }) => {
  await mockOps(page, "designer");
  await signIn(page);
  await expect(nav(page).getByRole("button")).toHaveText(["評価"]);
  await expect(page.getByRole("heading", { name: "評価の集計" })).toBeVisible();
  await page.getByRole("button", { name: "サインアウト" }).click();

  await page.unrouteAll();
  await mockOps(page, "auditor");
  await signIn(page);
  await expect(nav(page).getByRole("button")).toHaveText(["監査ログ"]);
  await expect(page.getByText("許可 / ロール: サポート")).toBeVisible();
  await expect(page.getByText("メモを変更")).toBeVisible();
});

test("30分操作がなければ、画面を消してサインアウトする", async ({ page }) => {
  await page.clock.install();
  await mockOps(page, "admin");
  await signIn(page);
  await expect(page.getByRole("heading", { name: "報告" })).toBeVisible();

  // 29分では、まだ消さない
  await page.clock.runFor("29:00");
  await expect(page.getByRole("heading", { name: "報告" })).toBeVisible();
  // 操作すると、数え直す
  await page.mouse.click(5, 300);
  await page.clock.runFor("29:00");
  await expect(page.getByRole("heading", { name: "報告" })).toBeVisible();
  await page.clock.runFor("01:30");
  await expect(page.getByText("30分操作がなかったため、画面を閉じてサインアウトしました。")).toBeVisible();
  await expect(page.getByRole("heading", { name: "報告" })).toHaveCount(0);
  await expect(page.getByText(REPORT.user)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "サインイン" })).toBeVisible();
});

test("一般ユーザーの画面から、運営者の画面へのリンクはない", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "メールでサインイン" })).toBeVisible();
  await expect(page.locator('a[href*="/ops"]')).toHaveCount(0);
  await expect(page.getByText("運営")).toHaveCount(0);
});
