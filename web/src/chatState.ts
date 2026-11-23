// 会話の画面の状態(逐次表示のイベントを、画面の吹き出しに反映する。本体仕様書 §15.1 の M7)。
// 画面の部品から切り離し、イベントの順番ごとの動き(文字を足す、取り消す、置き換える)を、テストで確かめられるようにする。

export type Bubble =
  | { id: number; kind: "user"; text: string; read: boolean }
  | {
      id: number;
      kind: "character";
      text: string;
      status: "typing" | "streaming" | "final";
      variant: "message" | "silence" | "backchannel";
      sessionId?: string;
      turnNo?: number;
      proactive?: boolean; // キャラクタから話しかけたメッセージ
    }
  | { id: number; kind: "notice"; text: string }
  | { id: number; kind: "error"; text: string };

export type ChatState = { bubbles: Bubble[]; nextId: number; busy: boolean };

export const initialChat: ChatState = { bubbles: [], nextId: 1, busy: false };

type Event = { event: string; data: unknown };

/** 発話を送った直後の状態。 */
export function sent(state: ChatState, text: string): ChatState {
  return {
    bubbles: [...state.bubbles, { id: state.nextId, kind: "user", text, read: false }],
    nextId: state.nextId + 1,
    busy: true,
  };
}

/** キャラクタから話しかけたメッセージを、会話の最初に置く(評価・報告のため、セッションとターン番号を付ける)。 */
export function proactive(state: ChatState, message: { text: string; sessionId: string; turnNo: number }): ChatState {
  const bubble: Bubble = {
    id: state.nextId,
    kind: "character",
    text: message.text,
    status: "final",
    variant: "message",
    sessionId: message.sessionId,
    turnNo: message.turnNo,
    proactive: true,
  };
  return { ...state, bubbles: [...state.bubbles, bubble], nextId: state.nextId + 1 };
}

/** サーバーから読み直した、いまの会話の続きを、吹き出しにする(画面を開き直したとき)。 */
export function restore(
  sessionId: string,
  turns: { turn_no: number; user_input: string; reply_text: string | null; response_mode: string }[],
): ChatState {
  const bubbles: Bubble[] = [];
  let id = 1;
  for (const turn of turns) {
    const target = { sessionId, turnNo: turn.turn_no };
    if (turn.response_mode === "proactive") {
      bubbles.push({ id: id++, kind: "character", text: turn.reply_text ?? "", status: "final", variant: "message", ...target, proactive: true });
      continue;
    }
    if (turn.user_input) bubbles.push({ id: id++, kind: "user", text: turn.user_input, read: true });
    if (turn.response_mode === "notice") continue; // 通知の文面は、読み直さない(その場の案内)
    const variant = turn.response_mode === "silence" ? "silence" : turn.response_mode === "backchannel" ? "backchannel" : "message";
    const text = variant === "silence" ? "" : (turn.reply_text ?? "");
    bubbles.push({ id: id++, kind: "character", text, status: "final", variant, ...target });
  }
  return { bubbles, nextId: id, busy: false };
}

/** 逐次表示のイベントを1件、反映する。 */
export function apply(state: ChatState, { event, data }: Event): ChatState {
  const body = (data ?? {}) as Record<string, unknown>;
  const bubbles = [...state.bubbles];
  const lastUser = findLast(bubbles, (b) => b.kind === "user");
  const current = findLast(bubbles, (b) => b.kind === "character" && b.status !== "final");
  const push = (bubble: Bubble) => ({ ...state, bubbles: [...bubbles, bubble], nextId: state.nextId + 1 });
  const update = (index: number, bubble: Bubble) => {
    bubbles[index] = bubble;
    return { ...state, bubbles };
  };

  switch (event) {
    case "accepted":
      // 既読
      return lastUser >= 0 ? update(lastUser, { ...(bubbles[lastUser] as Bubble & { kind: "user" }), read: true }) : state;
    case "typing":
      return push({ id: state.nextId, kind: "character", text: "", status: "typing", variant: "message" });
    case "delta": {
      if (current < 0) return state;
      const bubble = bubbles[current] as Bubble & { kind: "character" };
      return update(current, { ...bubble, text: bubble.text + String(body.text ?? ""), status: "streaming" });
    }
    case "retract": {
      // 流した文字を取り消す(確認に引っかかった、最終の応答が違う)。入力中の表示に戻す
      if (current < 0) return state;
      const bubble = bubbles[current] as Bubble & { kind: "character" };
      return update(current, { ...bubble, text: "", status: "typing" });
    }
    case "message":
    case "silence":
    case "backchannel": {
      const variant = event === "message" ? "message" : event;
      const text = event === "silence" ? "" : String(body.text ?? "");
      if (current >= 0) {
        const bubble = bubbles[current] as Bubble & { kind: "character" };
        return update(current, { ...bubble, text, status: "final", variant });
      }
      return push({ id: state.nextId, kind: "character", text, status: "final", variant });
    }
    case "notice":
      return push({ id: state.nextId, kind: "notice", text: String(body.text ?? "") });
    case "done": {
      // このターンの吹き出しに、評価・報告のためのセッションとターン番号を付ける
      const sessionId = String(body.session_id ?? "");
      const turnNo = Number(body.turn_no ?? 0);
      const next = bubbles.map((b) =>
        b.kind === "character" && b.status !== "final" ? { ...b, status: "final" as const } : b,
      );
      for (let i = next.length - 1; i >= 0; i--) {
        const b = next[i];
        if (b.kind === "user") break;
        if (b.kind === "character") next[i] = { ...b, sessionId, turnNo };
      }
      return { ...state, bubbles: next, busy: false };
    }
    case "error": {
      // 入力中のままの吹き出しを消して、案内を出す
      const kept = bubbles.filter((b) => !(b.kind === "character" && b.status !== "final"));
      return {
        bubbles: [...kept, { id: state.nextId, kind: "error", text: String(body.message ?? "応答を作れませんでした。") }],
        nextId: state.nextId + 1,
        busy: false,
      };
    }
    default:
      return state;
  }
}

function findLast(bubbles: Bubble[], match: (b: Bubble) => boolean): number {
  for (let i = bubbles.length - 1; i >= 0; i--) if (match(bubbles[i])) return i;
  return -1;
}
