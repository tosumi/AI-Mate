// API の呼び出し(本体仕様書 §10.1、§15.1 の M7)。トークンは Authorization ヘッダーで送る(Cookie は使わない)。

import { type SseEvent, SseParser } from "./sse";

export type TokenSource = () => Promise<string>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export type Character = { character_id: string; name: string };
export type ConsentItem = { type: string; label: string; required: boolean; granted: boolean };
export type ConsentScreen = { id: string; title: string; text: string; items: ConsentItem[] };
export type ConsentStatus = { version: string; is_under_18: boolean; screens: ConsentScreen[]; missing: string[]; memory: boolean };

export type Memory = { id: string; tier: string; kind: string; summary: string; pinned: boolean; core: string | null; muted: boolean; source_day: string | null; period: string | null };
export type Topic = { id: string; kind: string; title: string; description: string; status: string; muted: boolean; entries: number; first_occurred?: string; last_occurred?: string };
export type Settings = {
  silence_enabled: boolean;
  silence_render: string;
  association_enabled: boolean;
  proactive_enabled: boolean;
  filter_mode?: string;
  is_under_18?: boolean;
};
// 各ターンのセッション(評価・報告の対象)。古いサーバーは付けないので、そのときは会話の session_id を使う
export type ConversationTurn = { session_id?: string; turn_no: number; user_input: string; reply_text: string | null; response_mode: string };
export type Conversation = { session_id: string | null; turns: ConversationTurn[] };
/** キャラクタから話しかけたメッセージ(まだ表示していないもの。§15.1 の M8)。 */
export type ProactiveMessage = { character_id: string; name: string; session_id: string; turn_no: number; text: string; ts: string };

export class Api {
  private readonly baseUrl: string;
  private readonly token: TokenSource;
  private readonly fetcher: typeof fetch;

  constructor(baseUrl: string, token: TokenSource, fetcher: typeof fetch = (...args) => fetch(...args)) {
    this.baseUrl = baseUrl;
    this.token = token;
    this.fetcher = fetcher;
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const response = await this.fetcher(`${this.baseUrl}${path}`, {
      method,
      headers: { Authorization: `Bearer ${await this.token()}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) throw await toError(response);
    return (await response.json()) as T;
  }

  characters(): Promise<Character[]> {
    return this.request("GET", "/v1/characters");
  }

  consentStatus(): Promise<ConsentStatus> {
    return this.request("GET", "/v1/account/consents");
  }

  grantConsents(types: string[]): Promise<ConsentStatus> {
    return this.request("POST", "/v1/account/consents", { types, channel: "web" });
  }

  declareAge(adult: boolean | null): Promise<unknown> {
    return this.request("POST", "/v1/account/age", { adult });
  }

  memories(characterId: string): Promise<Memory[]> {
    return this.request("GET", `/v1/memory?${scope(characterId)}`);
  }

  setMemoryMuted(characterId: string, id: string, muted: boolean) {
    return this.request("POST", `/v1/memory/${encodeURIComponent(id)}/${muted ? "mute" : "unmute"}?${scope(characterId)}`);
  }

  forgetMemory(characterId: string, id: string) {
    return this.request("DELETE", `/v1/memory/${encodeURIComponent(id)}?${scope(characterId)}`);
  }

  topics(characterId: string): Promise<{ topics: Topic[] }> {
    return this.request("GET", `/v1/topics?${scope(characterId)}`);
  }

  setTopicMuted(characterId: string, id: string, muted: boolean) {
    return this.request("POST", `/v1/topics/${encodeURIComponent(id)}/${muted ? "mute" : "unmute"}?${scope(characterId)}`);
  }

  archiveTopic(characterId: string, id: string) {
    return this.request("DELETE", `/v1/topics/${encodeURIComponent(id)}?${scope(characterId)}`);
  }

  settings(): Promise<Settings> {
    return this.request("GET", "/v1/settings");
  }

  updateSettings(
    changes: Partial<Pick<Settings, "silence_enabled" | "silence_render" | "association_enabled" | "proactive_enabled">>,
  ): Promise<Settings> {
    return this.request("PUT", "/v1/settings", changes);
  }

  pushConfig(): Promise<{ enabled: boolean; public_key: string | null }> {
    return this.request("GET", "/v1/push/config");
  }

  subscribePush(subscription: PushSubscriptionJSON): Promise<{ subscribed: boolean }> {
    return this.request("POST", "/v1/push/subscriptions", subscription);
  }

  unsubscribePush(endpoint: string): Promise<{ unsubscribed: boolean }> {
    return this.request("POST", "/v1/push/unsubscribe", { endpoint });
  }

  /** いまの会話の続き(画面を開き直したときに表示する。無入力で終わった会話は空)。 */
  conversation(characterId: string): Promise<Conversation> {
    return this.request("GET", `/v1/conversation?${scope(characterId)}`);
  }

  proactiveMessages(): Promise<{ messages: ProactiveMessage[] }> {
    return this.request("GET", "/v1/proactive");
  }

  markProactiveSeen(characterId: string, sessionId: string): Promise<{ seen: boolean }> {
    return this.request("POST", `/v1/proactive/${encodeURIComponent(characterId)}/seen`, { session_id: sessionId });
  }

  exportData(): Promise<unknown> {
    return this.request("GET", "/v1/account/export");
  }

  revokeConsent(kind: string): Promise<ConsentStatus> {
    return this.request("DELETE", `/v1/account/consents/${encodeURIComponent(kind)}`);
  }

  deleteAccount(): Promise<{ deleted: boolean; completed_at: string }> {
    return this.request("DELETE", "/v1/account?confirm=delete");
  }

  rate(body: {
    character_id: string; session_id: string; turn_no: number; overall: number;
    persona_fit?: number; naturalness?: number; empathy?: number; note?: string;
  }) {
    return this.request("POST", "/v1/ratings", body);
  }

  report(body: { character_id: string; session_id: string; turn_no: number; reason: string; note?: string }) {
    return this.request("POST", "/v1/reports", body);
  }

  /** 会話の1ターン(逐次表示)。イベントを、届いた順に `onEvent` に渡す。 */
  async streamTurn(characterId: string, text: string, onEvent: (event: SseEvent) => void, signal?: AbortSignal) {
    const response = await this.fetcher(`${this.baseUrl}/v1/turns/stream`, {
      method: "POST",
      headers: { Authorization: `Bearer ${await this.token()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ channel: "web", character_id: characterId, text }),
      signal,
    });
    if (!response.ok || !response.body) throw await toError(response);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SseParser();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const event of parser.feed(value)) onEvent(event);
    }
  }
}

function scope(characterId: string): string {
  return `character_id=${encodeURIComponent(characterId)}`;
}

async function toError(response: Response): Promise<ApiError> {
  let code = "http_error";
  let message = "通信に失敗しました。少し待って、もう一度お試しください。";
  try {
    const body = (await response.json()) as { code?: string; message?: string; detail?: string };
    code = body.code ?? code;
    message = body.message ?? body.detail ?? message;
  } catch {
    // 本文が JSON でないときは、既定の文面
  }
  if (response.status === 429) message = "少し間をおいてから、もう一度送ってください。";
  return new ApiError(response.status, code, message);
}
