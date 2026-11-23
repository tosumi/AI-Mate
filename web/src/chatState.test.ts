// 会話の画面の状態と、SSE の読み取りのテスト(本体仕様書 §15.1 の M7)。

import { describe, expect, it } from "vitest";
import { type ChatState, apply, initialChat, proactive, restore, sent } from "./chatState";
import { SseParser } from "./sse";

function run(events: [string, unknown][], start: ChatState = sent(initialChat, "こんにちは")): ChatState {
  return events.reduce((state, [event, data]) => apply(state, { event, data }), start);
}

describe("SSE の読み取り", () => {
  it("断片に分かれて届いても、イベントを順にそろえる", () => {
    const body = 'event: accepted\ndata: {}\n\nevent: delta\ndata: {"text":"こん"}\n\nevent: delta\ndata: {"text":"にちは"}\n\n';
    const parser = new SseParser();
    const events = [];
    for (let i = 0; i < body.length; i += 5) events.push(...parser.feed(body.slice(i, i + 5)));
    expect(events.map((e) => e.event)).toEqual(["accepted", "delta", "delta"]);
    expect(events.map((e) => (e.data as { text?: string }).text ?? "").join("")).toBe("こんにちは");
  });
});

describe("会話の画面の状態", () => {
  it("既読 → 入力中 → 文字を足す → 最終の応答 → ターン番号", () => {
    const state = run([
      ["accepted", {}],
      ["typing", {}],
      ["delta", { text: "やあ、" }],
      ["delta", { text: "元気?" }],
      ["message", { type: "message", text: "やあ、元気?" }],
      ["done", { session_id: "s1", turn_no: 3 }],
    ]);
    const [user, reply] = state.bubbles;
    expect(user).toMatchObject({ kind: "user", read: true });
    expect(reply).toMatchObject({ kind: "character", text: "やあ、元気?", status: "final", sessionId: "s1", turnNo: 3 });
    expect(state.busy).toBe(false);
  });

  it("取り消しで流した文字を消し、最終の応答に置き換える", () => {
    const mid = run([
      ["typing", {}],
      ["delta", { text: "危ない" }],
      ["retract", { reason: "check" }],
    ]);
    expect(mid.bubbles[1]).toMatchObject({ text: "", status: "typing" });
    const done = run([["message", { text: "安全な応答" }], ["done", { session_id: "s", turn_no: 1 }]], mid);
    expect(done.bubbles[1]).toMatchObject({ text: "安全な応答", status: "final" });
    expect(done.bubbles).toHaveLength(2);
  });

  it("無言・相づち・通知・失敗を表示する", () => {
    const silent = run([["silence", { type: "silence" }], ["done", { session_id: "s", turn_no: 2 }]]);
    expect(silent.bubbles[1]).toMatchObject({ kind: "character", variant: "silence", text: "" });
    const notice = run([
      ["typing", {}],
      ["message", { text: "話してくれてありがとう" }],
      ["notice", { kind: "crisis_resources", text: "相談窓口があります" }],
    ]);
    expect(notice.bubbles.map((b) => b.kind)).toEqual(["user", "character", "notice"]);
    const failed = run([["typing", {}], ["error", { code: "llm_unavailable", message: "いま応答を作れませんでした。" }]]);
    expect(failed.bubbles.map((b) => b.kind)).toEqual(["user", "error"]);
    expect(failed.busy).toBe(false);
  });
});

describe("キャラクタから話しかけたメッセージ", () => {
  it("会話の最初に置き、評価・報告のためのセッションとターン番号を付ける。返事は続けて表示する", () => {
    const opened = proactive(initialChat, { text: "そういえば、面接どうだった?", sessionId: "s1", turnNo: 1 });
    expect(opened.bubbles).toEqual([
      { id: 1, kind: "character", text: "そういえば、面接どうだった?", status: "final", variant: "message", sessionId: "s1", turnNo: 1, proactive: true },
    ]);
    const after = run(
      [["accepted", {}], ["message", { text: "よかったね" }], ["done", { session_id: "s1", turn_no: 2 }]],
      sent(opened, "うまくいったよ"),
    );
    const characters = after.bubbles.filter((b) => b.kind === "character");
    expect(characters.map((b) => [b.text, b.turnNo])).toEqual([["そういえば、面接どうだった?", 1], ["よかったね", 2]]);
  });
});

describe("いまの会話の続きを読み直す", () => {
  it("自発発話・ふつうの応答・無言を吹き出しにし、評価のためのターン番号を付ける。通知だけのターンは出さない", () => {
    const state = restore("s1", [
      { turn_no: 1, user_input: "", reply_text: "そういえば、面接どうだった?", response_mode: "proactive" },
      { turn_no: 2, user_input: "うまくいったよ", reply_text: "よかったね", response_mode: "full" },
      { turn_no: 3, user_input: "うん", reply_text: null, response_mode: "silence" },
      { turn_no: 4, user_input: "相談したい", reply_text: null, response_mode: "notice" },
    ]);
    expect(state.bubbles.map((b) => [b.kind, b.text, "turnNo" in b ? b.turnNo : null])).toEqual([
      ["character", "そういえば、面接どうだった?", 1],
      ["user", "うまくいったよ", null],
      ["character", "よかったね", 2],
      ["user", "うん", null],
      ["character", "", 3],
      ["user", "相談したい", null],
    ]);
    expect(state.busy).toBe(false);
    expect(state.nextId).toBe(7);
  });
});
