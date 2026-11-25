// ペルソナデザイナの画面(/designer。本体仕様書 §15.1 の M10 の手順3)の通しのテスト。API はテストの中で差し替える。

import { expect, type Page, test } from "@playwright/test";
import { API } from "./mockApi";

type Recorded = { checks: Record<string, unknown>[]; simulations: unknown[]; dumps: Record<string, unknown>[] };

const TYPES = [
  { type_id: "iclp", name: "静観家", composition: "内向・具体・論理・計画", axes: { i: 0.8, a: 0.2, e: 0.2, f: 0.2 }, style_card: "短く要点を話す。", defaults: { introversion: 0.8, talkativeness: 0.35, articulation: 0.7, expressiveness: 0.43, initiative: 0.28, relationship_sensitivity: 0.5 } },
  { type_id: "oaef", name: "語り手", composition: "外向・抽象・共感・柔軟", axes: { i: 0.2, a: 0.8, e: 0.8, f: 0.8 }, style_card: "連想を広げて話す。", defaults: { introversion: 0.2, talkativeness: 0.65, articulation: 0.7, expressiveness: 0.67, initiative: 0.52, relationship_sensitivity: 0.5 } },
];

function definition(id: string, name: string): Record<string, unknown> {
  return {
    schema_version: 1, character_id: id, version: "0.1.0", status: "draft", name, concept: { one_line: "" },
    persona_prompt: "静かに寄り添う。", speech_style: "短く話す。",
    base_personality: { type: "iclp", axes: { i: 0.8, a: 0.2, e: 0.2, f: 0.2 }, source: "preset", quiz: null },
    profile: { preset: null, deltas: {} }, overrides: {}, initial_relationship: 0,
    adaptation: { adaptability: 0.5, adjustable: ["humor"], fixed: ["first_person"] }, preferences: [],
    temperament: { baseline_vad: { v: 0, a: 0, d: 0 }, reactivity: 0.5, recovery_rate: 0.3 },
    silence_policy: { allow_anger_silence: false, max_consecutive: 2 }, appraisal_rules: [], event_rules: [],
    likes_dislikes: { catalog_version: 2, liked: {}, disliked: {} },
  };
}

export async function mockDesigner(page: Page): Promise<Recorded> {
  const recorded: Recorded = { checks: [], simulations: [], dumps: [] };
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body), headers: cors() });
  await page.route(`${API}/v1/designer/**`, async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors() });
    if (request.headers()["authorization"] !== "Bearer E2E-ONLY-FAKE-AUTH") return route.fulfill(json({ detail: "unauthorized" }, 401));
    const path = new URL(request.url()).pathname.replace("/v1/designer", "");
    const body = request.postData() ? JSON.parse(request.postData() as string) : undefined;
    if (path === "/choices") {
      return route.fulfill(json({
        axes: [{ id: "i", name: "内向度", low: "外向", high: "内向" }, { id: "a", name: "抽象度", low: "具体", high: "抽象" }, { id: "e", name: "共感度", low: "論理", high: "共感" }, { id: "f", name: "柔軟度", low: "計画", high: "柔軟" }],
        types: TYPES,
        profiles: [{ id: "kamoku", name: "寡黙", deltas: { talkativeness: -0.2 }, initial_relationship: 0, temperament: { reactivity: 0.3, recovery_rate: 0.4, baseline_vad: { v: 0.1, a: -0.3, d: 0.2 } } }],
        emotions: ["joy", "trust", "fear", "surprise", "sadness", "disgust", "anger", "anticipation"],
        likes_catalog: { version: 2, categories: [{ id: "color", label: "色" }, { id: "hobby", label: "趣味・活動" }], items: [
          { id: "color.pink", category: "color", label: "ピンク", likeable: true, dislikeable: true, involvement: false, note: "" },
          { id: "hobby.reading", category: "hobby", label: "読書", likeable: true, dislikeable: true, involvement: false, note: "" },
        ] },
        event_triggers: [{ name: "gratitude", kind: "act", label: "感謝された", positive_only: false, requires_character_target: false }],
      }));
    }
    if (path === "/samples") return route.fulfill(json({ samples: [{ character_id: "sample_kamoku", name: "シズク" }] }));
    if (path === "/samples/sample_kamoku") {
      const def = definition("sample_kamoku", "シズク");
      return route.fulfill(json({ character_id: "sample_kamoku", read_only: true, definition: def, yaml: JSON.stringify(def) }));
    }
    if (path === "/new") return route.fulfill(json({ definition: definition(body.character_id, body.name) }));
    if (path === "/yaml/parse") return route.fulfill(json({ definition: JSON.parse(body.text) }));
    if (path === "/yaml/dump") {
      recorded.dumps.push(body.definition);
      return route.fulfill(json({ yaml: JSON.stringify(body.definition) }));
    }
    if (path === "/check") {
      const def = body.definition as Record<string, unknown>;
      recorded.checks.push(def);
      const bad = String(def.persona_prompt ?? "").includes("禁止");
      const delta = ((def.profile as { deltas?: Record<string, number> })?.deltas ?? {}).talkativeness ?? 0;
      return route.fulfill(json({
        ok: !bad,
        findings: bad ? [{ code: "E05", level: "error", message: "禁止された指示が含まれています", path: "persona_prompt" }] : [{ code: "W21", level: "warning", message: "頭打ちになります", path: "profile" }],
        uses_sample_id: String(def.character_id).startsWith("sample_"),
        resolved: {
          type_id: "iclp", type_from_axes: "iclp", traits: { ...TYPES[0].defaults, talkativeness: Math.max(0, Math.min(1, 0.35 + delta)) }, defaults: TYPES[0].defaults,
          association: { association_ease: 0.4, topic_shift: 0.3 }, style_card: "短く要点を話す。", preferences: [],
          curve: [-20, -10, 0, 10, 20].map((r) => ({ relationship: r, talkativeness_eff: 0.35, expressiveness_eff: 0.43, length: "medium", expression_threshold: 0.48, expression_cap: 0.6 })),
          sync_curve: [-20, -10, 0, 10, 20].map((r) => ({ relationship: r, weight: Math.max(0, r / 40) })), half_life_turns: 1.94, sheet_rules: [],
        },
      }));
    }
    if (path === "/simulate") {
      recorded.simulations.push(body);
      return route.fulfill(json({ ok: true, runs: [{ relationship: null, modes: { full: 0.6, brief: 0.2, backchannel: 0.1, silence: 0.1 }, passed: 2, total: 2, scenarios: [
        { scenario: "S1", title: "ふつうの会話", ok: true, violations: [], failures: [], modes: { full: 3, brief: 1 }, turns: [] },
        { scenario: "S2", title: "落ち込み", ok: true, violations: [], failures: [], modes: { brief: 2 }, turns: [] },
      ] }] }));
    }
    return route.fulfill(json({ detail: "not found" }, 404));
  });
  return recorded;
}

function cors() {
  return { "access-control-allow-origin": "http://localhost:5174", "access-control-allow-headers": "authorization,content-type", "access-control-allow-methods": "GET,POST" };
}

export async function signIn(page: Page) {
  await page.goto("/designer");
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await expect(page.getByRole("heading", { name: "使い方を選んでください" })).toBeVisible();
}

test("最初に使い方を選ぶ(既定はデザイナ。改行1回で進める)。新しく作り、値を動かすとプレビューが変わる", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await expect(page.getByRole("button", { name: /^デザイナ/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "はじめる" })).toBeVisible();

  await page.getByLabel("名前").fill("ミオ");
  await page.getByRole("button", { name: "作る" }).click();
  await expect(page.getByRole("complementary", { name: "プレビュー" }).getByText("エラーなし・警告 1件・情報 0件")).toBeVisible();
  await expect(page.getByRole("img", { name: /レーダーチャート/ })).toBeVisible();

  await page.getByRole("button", { name: /^表出/ }).click();
  await page.getByLabel("口数(数値)").fill("0.3");
  await expect.poll(() => recorded.checks.some((d) => (d.profile as { deltas: Record<string, number> }).deltas.talkativeness === 0.3)).toBe(true);
  await expect(page.getByRole("row", { name: /口数 0\.65/ })).toBeVisible();

  await page.getByRole("button", { name: /^人格・話し方/ }).click();
  await page.getByLabel("性格ディレクティブ").fill("禁止の指示");
  await expect(page.getByText("禁止された指示が含まれています")).toBeVisible();
  await expect(page.getByText("エラー 1件", { exact: false })).toBeVisible();
});

test("ウィザードで、サンプルから作り、確認でシミュレーションし、自分の ID で保存する", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await page.getByRole("button", { name: /^ウィザード/ }).click();
  await page.getByRole("button", { name: "シズクを開く" }).click();
  await expect(page.getByRole("heading", { name: "1 / 8: 基本" })).toBeVisible();
  await expect(page.getByText("サンプルの ID のままです")).toBeVisible();
  for (let i = 0; i < 7; i += 1) await page.getByRole("button", { name: "次へ" }).click();
  await expect(page.getByRole("heading", { name: "8 / 8: 確認" })).toBeVisible();
  await page.getByRole("button", { name: "実行する" }).click();
  await expect(page.getByText("すべて合格")).toBeVisible();
  expect(recorded.simulations).toHaveLength(1);

  await page.getByRole("button", { name: "YAML を保存" }).last().click();
  await expect(page.getByRole("dialog", { name: "自分の ID で保存" })).toBeVisible();
  await page.getByLabel("新しい ID").fill("my_shizuku");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "この ID で保存" }).click();
  expect((await download).suggestedFilename()).toBe("my_shizuku.persona.yaml");
  expect(recorded.dumps.at(-1)?.character_id).toBe("my_shizuku");
});

test("下書きは再読み込みで消えない。比較で、編集前との違いと、性格タイプどうしを見られる", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await page.keyboard.press("Enter");
  await page.getByLabel("名前").fill("ミオ");
  await page.getByRole("button", { name: "作る" }).click();
  await page.getByLabel("名前").fill("ミオ改");
  await page.reload();
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: /^デザイナ/ }).click();
  await expect(page.getByLabel("名前")).toHaveValue("ミオ改");

  await page.getByRole("navigation", { name: "デザイナの画面" }).getByRole("button", { name: "比較" }).click();
  await expect(page.getByRole("row", { name: /^name ミオ ミオ改$/ })).toBeVisible();
  await page.getByRole("button", { name: "性格タイプどうし" }).click();
  await expect(page.getByRole("cell", { name: "外向・抽象・共感・柔軟" })).toBeVisible();
});

test("YAML のファイルを開ける(CLI の persona.yaml と同じ形)", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await page.keyboard.press("Enter");
  const file = JSON.stringify(definition("from_file", "ファイルの子"));
  await page.getByText("ファイルを選ぶ").locator("input[type=file]").setInputFiles({ name: "from_file.persona.yaml", mimeType: "text/yaml", buffer: Buffer.from(file) });
  await expect(page.getByText("ファイルの子(from_file)・from_file.persona.yaml")).toBeVisible();
});
