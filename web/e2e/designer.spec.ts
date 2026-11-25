// ペルソナデザイナの画面(/designer。本体仕様書 §15.1 の M10 の手順3、M10-2 の手順2)の通しのテスト。API はテストの中で差し替える。

import { expect, type Page, test } from "@playwright/test";
import { API } from "./mockApi";

type Recorded = {
  checks: Record<string, unknown>[]; simulations: unknown[]; dumps: Record<string, unknown>[];
  wizard: { character_id: string; name: string; answers: Record<string, unknown>; finish?: boolean }[];
};

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

// ウィザードの質問(サーバーが返す形。テストでは3問だけ)
const WIZARD_QUESTIONS = [
  { id: "co_role", section: "concept", section_label: "役どころ", kind: "choice", text: "{name}は、話し相手にとって、どんな存在ですか。", note: "", default: "friend",
    options: [{ value: "friend", label: "気の合う友だち" }, { value: "listener", label: "話を聞いてくれる聞き役" }, { value: "cheerer", label: "元気をくれる存在" }] },
  { id: "i1", section: "type", section_label: "性格", kind: "scale", text: "{name}は、大人数の場より、少人数や一人の時間で元気を取り戻す。", note: "", default: 3, options: [] },
  { id: "lk_liked", section: "likes", section_label: "好き嫌い", kind: "likes", text: "{name}の好きなものを、区分ごとに1つまで選んでください。", note: "", default: {}, options: [] },
];

export async function mockDesigner(page: Page): Promise<Recorded> {
  const recorded: Recorded = { checks: [], simulations: [], dumps: [], wizard: [] };
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
    if (path === "/wizard") {
      recorded.wizard.push(body);
      const answered = Object.keys(body.answers).length;
      const done = body.finish || answered >= WIZARD_QUESTIONS.length;
      const step = {
        version: 1, sections: [{ id: "concept", label: "役どころ" }, { id: "type", label: "性格" }, { id: "likes", label: "好き嫌い" }],
        progress: { answered, total: WIZARD_QUESTIONS.length }, done,
        question: done ? null : { ...WIZARD_QUESTIONS[answered], text: WIZARD_QUESTIONS[answered].text.replace("{name}", body.name) },
      };
      if (!done) return route.fulfill(json(step));
      const def = definition(body.character_id, body.name);
      return route.fulfill(json({ ...step, definition: def, acknowledged: [], defaulted: WIZARD_QUESTIONS.length - answered,
        check: { ok: true, findings: [{ code: "W21", level: "warning", message: "頭打ちになります", path: "profile.deltas.relationship_sensitivity" }], uses_sample_id: false, resolved: null } }));
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
  await expect(page.getByRole("heading", { name: "データの確認" })).toBeVisible();
}

/** 新規作成: ID と名前を入れて、作り方の選択まで進む。 */
async function startNew(page: Page, name = "ミオ") {
  await page.getByRole("button", { name: "新しく作る" }).click();
  await page.getByLabel("名前", { exact: true }).fill(name);
  await page.getByRole("button", { name: "次へ" }).click();
  await expect(page.getByRole("heading", { name: "作り方を選んでください" })).toBeVisible();
}

test("トップはデータの確認。新規作成 → ID と名前 → 作り方(既定はウィザード。改行1回)→ 質問に答えるだけでできあがり、保存できる", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await expect(page.getByRole("button", { name: /^ウィザード/ })).toBeFocused();
  await expect(page.getByText("あとから切り替えられます")).toHaveCount(0);
  await page.keyboard.press("Enter");

  await expect(page.getByRole("group", { name: "ミオは、話し相手にとって、どんな存在ですか。" })).toBeVisible();
  await expect(page.getByText("質問 1 / 3")).toBeVisible();
  // 右下の「次へ」は、いつも出ている。選ばれている回答(初めてなら既定の回答)で進む
  await expect(page.getByRole("button", { name: "気の合う友だち" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "次へ" }).click();
  await expect(page.getByText("質問 2 / 3")).toBeVisible();
  expect(recorded.wizard.at(-1)?.answers).toEqual({ co_role: "friend" });
  await page.getByRole("button", { name: "戻る" }).click();
  await page.getByRole("button", { name: "話を聞いてくれる聞き役" }).click();
  await page.getByRole("button", { name: "やや当てはまる" }).click();
  // 戻ると、選んだ回答に印が付いたまま、同じ質問をもう一度出す
  await expect(page.getByRole("group", { name: /好きなもの/ })).toBeVisible();
  await page.getByRole("button", { name: "戻る" }).click();
  await expect(page.getByRole("button", { name: "やや当てはまる" })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "とても当てはまる" }).click();
  await page.getByLabel("色").selectOption("color.pink");
  await expect(page.getByRole("group", { name: /好きなもの/ }).getByRole("button", { name: "次へ" })).toHaveCount(0);
  await page.getByRole("button", { name: "次へ" }).click();

  await expect(page.getByRole("heading", { name: "できあがりました" })).toBeVisible();
  expect(recorded.wizard.at(-1)?.answers).toEqual({ co_role: "listener", i1: 5, lk_liked: { color: "color.pink" } });
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "YAML を保存" }).click();
  expect((await download).suggestedFilename()).toBe("my_character.persona.yaml");

  // できあがりの画面: 「最後の質問に戻る」「最初からやり直す」があり、上の「新しく始める」は出さない
  await expect(page.getByRole("button", { name: "最後の質問に戻る" })).toBeVisible();
  await expect(page.getByRole("button", { name: "最初からやり直す" })).toBeVisible();
  await expect(page.getByRole("button", { name: "新しく始める" })).toHaveCount(0);

  // 「デザイナで細かく調整する」も、切り替えの確認を出す(やめると、この画面のまま)
  await page.getByRole("button", { name: "デザイナで細かく調整する" }).click();
  await expect(page.getByRole("dialog", { name: "デザイナに切り替えますか" })).toBeVisible();
  await page.getByRole("button", { name: "やめる(この画面に戻る)" }).click();
  await expect(page.getByRole("dialog", { name: "デザイナに切り替えますか" })).toHaveCount(0);

  // できあがりの画面の検査の結果を押すと、切り替えの確認のあと、デザイナのその項目へ移る(やめると、この画面のまま)
  await page.getByRole("complementary", { name: "プレビュー" }).getByRole("button", { name: /W21/ }).click();
  const confirm = page.getByRole("dialog", { name: "デザイナに切り替えますか" });
  await expect(confirm.getByText("ウィザードの回答には戻れません")).toBeVisible();
  await confirm.getByRole("button", { name: "やめる(この画面に戻る)" }).click();
  await expect(page.getByRole("heading", { name: "できあがりました" })).toBeVisible();
  await page.getByRole("complementary", { name: "プレビュー" }).getByRole("button", { name: /W21/ }).click();
  await expect(page.getByRole("button", { name: "デザイナに切り替える" })).toBeFocused();
  await page.getByRole("button", { name: "デザイナに切り替える" }).click();
  await expect(page.getByRole("heading", { name: "表出", exact: true })).toBeVisible();
  await expect(page.getByRole("slider", { name: "関係値への感応度", exact: true })).toBeFocused(); // 読み上げの名前には、YAML の名前を含めない
});

test("ウィザードの途中でデザイナに切り替えるときは、残りを既定の回答にしてよいかを確かめる(やめると質問を続ける)", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "気の合う友だち" }).click();

  await page.getByRole("button", { name: "詳細設定(デザイナ)に切り替え" }).click();
  await expect(page.getByRole("dialog", { name: "デザイナに切り替えますか" }).getByText("残りの 2 問")).toBeVisible();
  // 確認が出ている間は、後ろの画面を操作できない(押しても、次の質問に進まない)
  await page.getByRole("button", { name: "気の合う友だち" }).click({ force: true, timeout: 2000 }).catch(() => {});
  await page.getByRole("button", { name: "新しく始める" }).click({ force: true, timeout: 2000 }).catch(() => {});
  await expect(page.getByRole("dialog", { name: "デザイナに切り替えますか" })).toBeVisible();
  expect(recorded.wizard.at(-1)?.answers).toEqual({ co_role: "friend" });
  // Esc は「やめる」と同じ
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "デザイナに切り替えますか" })).toHaveCount(0);
  await page.getByRole("button", { name: "詳細設定(デザイナ)に切り替え" }).click();
  await page.getByRole("button", { name: "やめる(質問を続ける)" }).click();
  await expect(page.getByRole("group", { name: /ミオは、大人数の場より/ })).toBeVisible();

  await page.getByRole("button", { name: "詳細設定(デザイナ)に切り替え" }).click();
  await page.getByRole("button", { name: "既定の回答で組み立てて、切り替える" }).click();
  await expect(page.getByRole("navigation", { name: "設定の項目" })).toBeVisible();
  await expect(page.getByText("ミオ(my_character)", { exact: true })).toBeVisible();
  expect(recorded.wizard.at(-1)).toMatchObject({ finish: true, answers: { co_role: "friend" } });
});

test("デザイナ(詳細設定)で新しく作り、値を動かすとプレビューが変わる", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.getByRole("button", { name: /^デザイナ/ }).click();
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

test("検査の結果(Exx・Wxx)を押すと、原因の設定の項目に移り、その欄に焦点が当たる", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.getByRole("button", { name: /^デザイナ/ }).click();
  const preview = page.getByRole("complementary", { name: "プレビュー" });

  // 警告 W21(場所 profile)→「表出」の、表出プロファイルの欄
  await preview.getByRole("button", { name: /W21/ }).click();
  await expect(page.getByRole("heading", { name: "表出", exact: true })).toBeVisible();
  await expect(page.getByLabel("プリセット")).toBeFocused();

  // 下までスクロールしても、上部の帯(名前と ID、ボタン)は見えたまま
  await page.mouse.wheel(0, 3000);
  await expect(page.getByText("ミオ(my_character)", { exact: true })).toBeInViewport();
  await expect(page.getByRole("button", { name: "YAML を保存" })).toBeInViewport();

  // エラー E05(場所 persona_prompt)→「人格・話し方」の、性格ディレクティブの欄
  await page.getByRole("button", { name: /^人格・話し方/ }).click();
  await page.getByLabel("性格ディレクティブ").fill("禁止の指示");
  await page.getByRole("button", { name: /^基本/ }).click();
  await preview.getByRole("button", { name: /E05/ }).click();
  await expect(page.getByRole("heading", { name: "人格・話し方", exact: true })).toBeVisible();
  await expect(page.getByLabel("性格ディレクティブ")).toBeFocused();
});

test("デザイナからウィザードに切り替えるときは、ID と名前のほかを消してよいかを確かめる", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.getByRole("button", { name: /^デザイナ/ }).click();
  await page.getByRole("button", { name: "ウィザードに切り替え" }).click();
  const dialog = page.getByRole("dialog", { name: "ウィザードに切り替えますか" });
  await expect(dialog.getByText("IDと名前以外は、すべて消えます。")).toBeVisible();
  await dialog.getByRole("button", { name: "やめる(デザイナを続ける)" }).click();
  await expect(page.getByRole("navigation", { name: "設定の項目" })).toBeVisible();

  await page.getByRole("button", { name: "ウィザードに切り替え" }).click();
  await page.getByRole("button", { name: "設定を消して、ウィザードを始める" }).click();
  await expect(page.getByRole("group", { name: "ミオは、話し相手にとって、どんな存在ですか。" })).toBeVisible();
  expect(recorded.wizard.at(-1)).toMatchObject({ character_id: "my_character", name: "ミオ", answers: {} });
});

test("保存データの読み込み: サンプルは自分の ID と名前のコピーとして、作り方を選ばずにデザイナで開く", async ({ page }) => {
  const recorded = await mockDesigner(page);
  await signIn(page);
  await page.getByRole("button", { name: "シズクをコピーして開く" }).click();
  const dialog = page.getByRole("dialog", { name: "シズクをコピーして開く" });
  await dialog.getByLabel("キャラクタの ID").fill("sample_x");
  await expect(dialog.getByRole("button", { name: "コピーして開く" })).toBeDisabled();
  await dialog.getByLabel("キャラクタの ID").fill("my_shizuku");
  await dialog.getByLabel("名前").fill("シズカ");
  await dialog.getByRole("button", { name: "コピーして開く" }).click();
  await expect(page.getByText("シズカ(my_shizuku)", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "作り方を選んでください" })).toHaveCount(0);

  await page.getByRole("navigation", { name: "デザイナの画面" }).getByRole("button", { name: "シミュレーション" }).click();
  await page.getByRole("button", { name: "実行する" }).click();
  await expect(page.getByText("すべて合格")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "YAML を保存" }).click();
  expect((await download).suggestedFilename()).toBe("my_shizuku.persona.yaml");
  expect(recorded.dumps.at(-1)).toMatchObject({ character_id: "my_shizuku", name: "シズカ" });
});

test("下書きは再読み込みで消えない。比較で、編集前との違いと、性格タイプどうしを見られる", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.getByRole("button", { name: /^デザイナ/ }).click();
  await page.getByLabel("名前").fill("ミオ改");
  await page.reload();
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "デザイナの下書きに戻る(ミオ改)" }).click();
  await expect(page.getByLabel("名前")).toHaveValue("ミオ改");

  await page.getByRole("navigation", { name: "デザイナの画面" }).getByRole("button", { name: "比較" }).click();
  await expect(page.getByRole("row", { name: /^name ミオ ミオ改$/ })).toBeVisible();
  await page.getByRole("button", { name: "性格タイプどうし" }).click();
  await expect(page.getByRole("cell", { name: "外向・抽象・共感・柔軟" })).toBeVisible();
});

test("ウィザードの途中も、再読み込みのあとに続きから答えられる", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "元気をくれる存在" }).click();
  await expect(page.getByRole("group", { name: /ミオは、大人数の場より/ })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "メールでサインイン" }).click();
  await page.getByRole("button", { name: "ウィザードの続きから(ミオ・1 問まで回答済み)" }).click();
  await expect(page.getByRole("group", { name: /ミオは、大人数の場より/ })).toBeVisible();
});

test("YAML のファイルを開くと、作り方を選ばずにデザイナで開く(CLI の persona.yaml と同じ形)", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  const file = JSON.stringify(definition("from_file", "ファイルの子"));
  await page.getByText("YAML のファイルを開く").locator("input[type=file]").setInputFiles({ name: "from_file.persona.yaml", mimeType: "text/yaml", buffer: Buffer.from(file) });
  await expect(page.getByText("ファイルの子(from_file)", { exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "設定の項目" })).toBeVisible();
});

test("ウィザードのできあがりの画面の「最初からやり直す」は、確認のあと、回答を消して1問目からの状態にし、データの確認へ戻る", async ({ page }) => {
  await mockDesigner(page);
  await signIn(page);
  await startNew(page);
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "詳細設定(デザイナ)に切り替え" }).waitFor();
  await page.getByRole("button", { name: "気の合う友だち" }).click();
  await page.getByRole("button", { name: "どちらともいえない" }).click();
  await page.getByRole("button", { name: "次へ" }).click();
  await expect(page.getByRole("heading", { name: "できあがりました" })).toBeVisible();
  await page.getByRole("button", { name: "最後の質問に戻る" }).click();
  await expect(page.getByRole("group", { name: /好きなもの/ })).toBeVisible();
  await page.getByRole("button", { name: "次へ" }).click();
  // 「いいえ」は、確認を閉じて、この画面のまま
  await page.getByRole("button", { name: "最初からやり直す" }).click();
  const ask = page.getByRole("dialog", { name: "最初からやり直しますか" });
  await expect(ask.getByText("すべての回答を既定値に戻して、最初の質問からやり直します。よろしいですか")).toBeVisible();
  await ask.getByRole("button", { name: "いいえ" }).click();
  await expect(ask).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "できあがりました" })).toBeVisible();

  // 「はい」は、回答を消して1問目からの状態として残し、最初の画面へ。続きからは1問目
  await page.getByRole("button", { name: "最初からやり直す" }).click();
  await page.getByRole("dialog", { name: "最初からやり直しますか" }).getByRole("button", { name: "はい" }).click();
  await expect(page.getByRole("heading", { name: "データの確認" })).toBeVisible();
  await page.getByRole("button", { name: "ウィザードの続きから(ミオ・0 問まで回答済み)" }).click();
  await expect(page.getByText("質問 1 / 3")).toBeVisible();
  await expect(page.getByRole("button", { name: "気の合う友だち" })).toHaveAttribute("aria-pressed", "true"); // 既定の回答
});
