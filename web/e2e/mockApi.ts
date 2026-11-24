// E2E 用の、差し替えの API(実在のサービスには接続しない)。同意の状況は、送った内容に応じて変わる。

import type { Page, Request } from "@playwright/test";

export const API = "http://api.e2e.test";

type Screen = { id: string; title: string; text: string; items: { type: string; label: string; required: boolean }[] };

const SCREENS: Screen[] = [
  { id: "1", title: "はじめに", text: "MyCompanion へようこそ", items: [
    { type: "terms", label: "[利用規約](【URL】)に同意します。", required: true },
    { type: "privacy", label: "プライバシーポリシーを確認しました。", required: true },
  ] },
  { id: "2", title: "感情の推定と記憶", text: "あなたの気持ちと、会話の記憶について", items: [
    { type: "emotion_data", label: "感情の推定に同意します。", required: true },
    { type: "memory", label: "記憶の保存に同意します。", required: true },
  ] },
  { id: "4", title: "年齢の確認", text: "あなたは 18歳以上ですか?", items: [] },
  { id: "5", title: "任意の設定", text: "お好みで選べる設定", items: [
    { type: "romantic_expression", label: "恋愛的な表現を有効にします。", required: false },
  ] },
];

export type Recorded = {
  consents: string[][]; age: (boolean | null)[]; ratings: unknown[]; reports: unknown[]; turns: string[];
  calls: string[]; settings: unknown[]; revoked: string[]; deleted: number; seen: unknown[];
  // 画面を開いたあとに、キャラクタが話しかける(サーバーの未表示のメッセージに加える)
  addProactive: (message: ProactiveFixture) => void;
};

/** キャラクタから話しかけたメッセージ(まだ表示していないもの)。 */
export type ProactiveFixture = { character_id: string; name: string; session_id: string; turn_no: number; text: string; ts: string };

export type StreamScript = (text: string) => { status?: number; events: [string, unknown][] };

/** API を差し替える。`stream` は、発話ごとに返す逐次表示のイベント。 */
export async function mockApi(
  page: Page,
  stream: StreamScript,
  opts: {
    consented?: boolean;
    proactive?: ProactiveFixture[];
    push?: boolean;
    // いまの会話の続き(キャラクタ → ターン)。画面を開き直したときに読み直す
    conversation?: Record<string, { session_id: string | null; turns: { session_id?: string; turn_no: number; user_input: string; reply_text: string | null; response_mode: string }[] }>;
  } = {},
): Promise<Recorded> {
  const recorded: Recorded = {
    consents: [], age: [], ratings: [], reports: [], turns: [], calls: [], settings: [], revoked: [], deleted: 0, seen: [],
    addProactive: (message) => proactive.push(message),
  };
  let proactive = [...(opts.proactive ?? [])];
  const memories = [
    { id: "m1", tier: "long", kind: "semantic", summary: "ユーザーはトモと呼ばれたい", pinned: true, core: "user_name", muted: false, source_day: null, period: null },
    { id: "m2", tier: "medium", kind: "episodic", summary: "ユーザーは猫を飼い始めた", pinned: false, core: null, muted: false, source_day: "2026-11-19", period: null },
  ];
  const topics = [{ id: "e-tama", kind: "entity", title: "猫", description: "飼い始めた猫", status: "active", muted: false, entries: 2 }];
  let settings = { silence_enabled: true, silence_render: "dots", association_enabled: true, proactive_enabled: false };
  const granted = new Set<string>(opts.consented ? SCREENS.flatMap((s) => s.items.filter((i) => i.required).map((i) => i.type)) : []);
  let answeredAge = !!opts.consented;

  const status = () => {
    const required = SCREENS.flatMap((s) => s.items.filter((i) => i.required).map((i) => i.type));
    return {
      version: "1.0-draft",
      is_under_18: false,
      screens: SCREENS.map((s) => ({ ...s, items: s.items.map((i) => ({ ...i, granted: granted.has(i.type) })) })),
      missing: [...required.filter((t) => !granted.has(t)), ...(answeredAge ? [] : [])],
      memory: granted.has("memory"),
    };
  };
  const json = (body: unknown, code = 200) => ({ status: code, contentType: "application/json", body: JSON.stringify(body) });
  const authorized = (request: Request) => request.headers()["authorization"] === "Bearer E2E-ONLY-FAKE-AUTH";

  await page.route(`${API}/**`, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: corsHeaders() });
    if (!authorized(request)) return route.fulfill({ ...json({ detail: "unauthorized" }, 401), headers: corsHeaders() });
    const body = request.postData() ? JSON.parse(request.postData() as string) : undefined;
    const reply = (payload: ReturnType<typeof json>) => route.fulfill({ ...payload, headers: corsHeaders() });

    recorded.calls.push(`${method} ${path}${new URL(request.url()).search}`);
    if (path === "/v1/account/consents" && method === "GET") return reply(json(status()));
    if (path.startsWith("/v1/account/consents/") && method === "DELETE") {
      const kind = decodeURIComponent(path.split("/").pop() as string);
      recorded.revoked.push(kind);
      granted.delete(kind);
      return reply(json(status()));
    }
    if (path === "/v1/memory" && method === "GET") return reply(json(memories));
    const memoryAction = path.match(/^\/v1\/memory\/([^/]+)(?:\/(mute|unmute))?$/);
    if (memoryAction) {
      const index = memories.findIndex((m) => m.id === memoryAction[1]);
      if (method === "DELETE") memories.splice(index, 1);
      else memories[index].muted = memoryAction[2] === "mute";
      return reply(json({ ok: true }));
    }
    if (path === "/v1/topics" && method === "GET") return reply(json({ topics }));
    const topicAction = path.match(/^\/v1\/topics\/([^/]+)(?:\/(mute|unmute))?$/);
    if (topicAction) {
      const index = topics.findIndex((t) => t.id === topicAction[1]);
      if (method === "DELETE") topics.splice(index, 1);
      else topics[index].muted = topicAction[2] === "mute";
      return reply(json({ ok: true }));
    }
    if (path === "/v1/proactive" && method === "GET") return reply(json({ messages: proactive }));
    if (path === "/v1/conversation" && method === "GET") {
      const id = new URL(request.url()).searchParams.get("character_id") ?? "";
      return reply(json(opts.conversation?.[id] ?? { session_id: null, turns: [] }));
    }
    if (path === "/v1/push/config") {
      // 通知が使えるときの公開鍵(テスト用の固定値。実在の鍵ではない)
      return reply(json(opts.push ? { enabled: true, public_key: "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" } : { enabled: false, public_key: null }));
    }
    const seenAction = path.match(/^\/v1\/proactive\/([^/]+)\/seen$/);
    if (seenAction && method === "POST") {
      recorded.seen.push({ character_id: decodeURIComponent(seenAction[1]), ...body });
      const before = proactive.length;
      proactive = proactive.filter((m) => !(m.character_id === decodeURIComponent(seenAction[1]) && m.session_id === body.session_id));
      return reply(json({ seen: proactive.length < before }));
    }
    if (path === "/v1/settings" && method === "GET") return reply(json(settings));
    if (path === "/v1/settings" && method === "PUT") {
      recorded.settings.push(body);
      settings = { ...settings, ...body };
      return reply(json(settings));
    }
    if (path === "/v1/account/export") return reply(json({ exported_at: "2026-11-20T00:00:00Z", characters: {} }));
    if (path === "/v1/account" && method === "DELETE") {
      recorded.deleted += 1;
      return reply(json({ deleted: true, completed_at: "2026-11-20T00:00:00Z" }));
    }
    if (path === "/v1/account/consents" && method === "POST") {
      recorded.consents.push(body.types);
      for (const type of body.types) granted.add(type);
      return reply(json(status()));
    }
    if (path === "/v1/account/age" && method === "POST") {
      recorded.age.push(body.adult);
      answeredAge = true;
      return reply(json({ is_under_18: body.adult !== true }));
    }
    if (path === "/v1/characters") return reply(json([{ character_id: "sample_oshaberi", name: "ハル" }, { character_id: "sample_kamoku", name: "シズク" }]));
    if (path === "/v1/ratings") {
      recorded.ratings.push(body);
      return reply(json({ rated: true, id: "r1" }));
    }
    if (path === "/v1/reports") {
      recorded.reports.push(body);
      return reply(json({ reported: true, id: "p1" }));
    }
    if (path === "/v1/turns/stream") {
      recorded.turns.push(body.text);
      const script = stream(body.text);
      if (script.status && script.status !== 200) return reply(json({ code: "rate_limited", message: "多すぎます" }, script.status));
      const sse = script.events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join("");
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse, headers: corsHeaders() });
    }
    return reply(json({ detail: "not found" }, 404));
  });
  return recorded;
}

function corsHeaders() {
  return {
    "access-control-allow-origin": "http://localhost:5174",
    "access-control-allow-headers": "authorization,content-type",
    "access-control-allow-methods": "GET,POST,PUT,DELETE",
  };
}

/** ふつうの応答: 既読 → 入力中 → 文字を分けて → 最終の応答 → 終わり。 */
export function normalReply(reply: string, turnNo = 1): [string, unknown][] {
  const chunks = reply.match(/.{1,4}/gsu) ?? [];
  return [
    ["accepted", {}],
    ["typing", {}],
    ...chunks.map((text) => ["delta", { text }] as [string, unknown]),
    ["message", { type: "message", text: reply, variant: "filtered" }],
    ["done", { session_id: "11111111-1111-1111-1111-111111111111", turn_no: turnNo, debug_marker: null }],
  ];
}
