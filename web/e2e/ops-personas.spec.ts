// 運営者の画面の「ペルソナ」タブ(本体仕様書 §15.1 の M10 の手順4)。API はテストの中で差し替える。

import { expect, type Page, test } from "@playwright/test";
import { API } from "./mockApi";

const PERMS: Record<string, string[]> = {
  designer: ["view_self", "view_ratings", "view_personas", "submit_personas", "preview_personas"],
  approver: ["view_self", "view_ratings", "view_personas", "publish_personas", "preview_personas"],
};
const SUB = "bbbbbbbb-0000-0000-0000-000000000001";

type Recorded = { calls: string[]; bodies: Record<string, unknown>[] };

async function mockPersonas(page: Page, role: string): Promise<Recorded> {
  const recorded: Recorded = { calls: [], bodies: [] };
  let pending = true;
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body), headers: cors() });
  await page.route(`${API}/ops/v1/**`, async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors() });
    const url = new URL(request.url());
    const path = url.pathname.replace("/ops/v1", "");
    const body = request.postData() ? JSON.parse(request.postData() as string) : undefined;
    recorded.calls.push(`${request.method()} ${path}`);
    if (body) recorded.bodies.push(body);
    if (path === "/me") return route.fulfill(json({ roles: [role], permissions: PERMS[role] }));
    if (path === "/ratings") return route.fulfill(json({ since: "2026-11-01T00:00:00+00:00", min_n: 5, rows: [] }));
    if (path === "/personas") {
      return route.fulfill(json({
        characters: [{ character_id: "my_char", name: "ミオ", versions: [
          { version: 2, version_label: "1.1.0", status: "published", created_at: "2026-11-25T01:00:00+00:00" },
          { version: 1, version_label: "1.0.0", status: "deprecated", created_at: "2026-11-24T01:00:00+00:00" },
        ] }],
        pending: [],
      }));
    }
    if (path === "/personas/submissions" && request.method() === "GET") {
      const status = url.searchParams.get("status");
      return route.fulfill(json({ submissions: status === "pending" && pending ? [{
        submission_id: SUB, character_id: "my_char", version_label: "1.2.0", name: "ミオ", submitted_by: "oid-designer-123",
        submitted_at: "2026-11-25T02:00:00+00:00", status: "pending", decided_by: null, decided_at: null, published_version: null,
      }] : [] }));
    }
    if (path === `/personas/submissions/${SUB}`) {
      return route.fulfill(json({
        submission_id: SUB, character_id: "my_char", version_label: "1.2.0", submitted_by: "oid-designer-123", submitted_at: "2026-11-25T02:00:00+00:00",
        status: "pending", decided_by: null, decided_at: null, published_version: null, notes: null,
        lint_json: [{ code: "W21", level: "warning", message: "頭打ちになります", path: "profile" }], simulation_summary: "mock: 20/20 合格",
        definition: { name: "ミオ", persona_prompt: "静かに寄り添う。", speech_style: "短く話す。" },
        current_version: { version: 2, version_label: "1.1.0" },
        changes: [{ path: "traits.talkativeness", before: 0.35, after: 0.45 }],
      }));
    }
    if (path === `/personas/submissions/${SUB}/approve`) {
      if (!body?.notes) return route.fulfill(json({ detail: "警告(W21)があるので、確認した理由のメモが必要です" }, 422));
      pending = false;
      return route.fulfill(json({ published_version: 3, character_id: "my_char", version_label: "1.2.0" }));
    }
    if (path === "/personas/my_char/rollback") return route.fulfill(json({ published_version: 1 }));
    if (path === "/personas/yaml") return route.fulfill(json({ definition: JSON.parse(body.text) }));
    if (path === "/personas/inspect") {
      return route.fulfill(json({ ok: true, findings: [], blocking: [], scenarios: { passed: 20, total: 20, failed: [], modes: {} } }));
    }
    if (path === "/personas/submissions" && request.method() === "POST") return route.fulfill(json({ submission_id: SUB, inspection: {} }));
    if (path === "/personas/scenarios") return route.fulfill(json({ scenarios: [{ id: "S1", title: "ふつうの会話" }] }));
    if (path === "/personas/preview/chat") {
      return route.fulfill(json({ session_id: "sess-1", actions: [{ type: "message", text: "こんにちは。今日はどうだった?" }] }));
    }
    return route.fulfill(json({ detail: "not found" }, 404));
  });
  return recorded;
}

function cors() {
  return { "access-control-allow-origin": "http://localhost:5174", "access-control-allow-headers": "authorization,content-type", "access-control-allow-methods": "GET,POST,PATCH" };
}

async function open(page: Page) {
  await page.goto("/ops");
  await page.getByRole("button", { name: "サインイン" }).click();
  await page.getByRole("navigation", { name: "運営の画面" }).getByRole("button", { name: "ペルソナ" }).click();
}

test("承認者は、提出を確かめ、メモを書いて公開する(メモがないと止まる)", async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  const recorded = await mockPersonas(page, "approver");
  await open(page);
  await page.getByRole("button", { name: "my_char 1.2.0 の提出を開く" }).click();
  const detail = page.getByRole("complementary", { name: "提出の詳細" });
  await expect(detail.getByText("頭打ちになります")).toBeVisible();
  await expect(detail.getByRole("cell", { name: "traits.talkativeness" })).toBeVisible();
  await detail.getByRole("button", { name: "承認して公開する" }).click();
  await expect(detail.getByText("確認した理由のメモが必要です")).toBeVisible();
  await detail.getByLabel(/確認のメモ/).fill("頭打ちは意図どおり");
  await detail.getByRole("button", { name: "承認して公開する" }).click();
  await expect(page.getByText("該当する提出はありません。")).toBeVisible();
  expect(recorded.bodies).toContainEqual({ notes: "頭打ちは意図どおり" });

  await page.getByRole("button", { name: "版の一覧" }).click();
  await page.getByRole("button", { name: "この版に戻す" }).click();
  await expect(page.getByText("戻しました(内部の版 1)。")).toBeVisible();
  expect(recorded.bodies).toContainEqual({ to: "1.0.0" });
});

test("設計者は、YAML を開いて検査し、提出し、実際のモデルで話してみる。公開の操作はない", async ({ page }) => {
  const recorded = await mockPersonas(page, "designer");
  await open(page);
  await expect(page.getByRole("button", { name: "版の一覧" })).toBeVisible();
  await page.getByRole("button", { name: "提出・試す" }).click();
  const definition = { character_id: "my_char", name: "ミオ", version: "1.2.0" };
  await page.getByText("YAML を開く").locator("input[type=file]").setInputFiles({ name: "my_char.persona.yaml", mimeType: "text/yaml", buffer: Buffer.from(JSON.stringify(definition)) });
  await expect(page.getByText("提出できます。")).toBeVisible();
  await page.getByRole("button", { name: "レビューに提出する" }).click();
  await expect(page.getByText("提出しました。")).toBeVisible();
  expect(recorded.calls).toContain("POST /personas/submissions");

  await page.getByLabel("試しの発話").fill("こんにちは");
  await page.getByRole("button", { name: "送る" }).click();
  await expect(page.getByText("キャラクタ: こんにちは。今日はどうだった?")).toBeVisible();

  await page.getByRole("button", { name: "レビュー", exact: true }).click();
  await page.getByRole("button", { name: "my_char 1.2.0 の提出を開く" }).click();
  await expect(page.getByRole("button", { name: "承認して公開する" })).toHaveCount(0);
});
