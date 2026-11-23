// 会話の画面(本体仕様書 §15.1 の M7)。逐次表示、既読・入力中、無言・相づち、通知、応答ごとの評価と報告。
// キャラクタごとに会話を残す(キャラクタやページを切り替えても消さない)。画面を開き直したときは、
// いまの会話の続きをサーバーから読み直す(会話の内容は、ブラウザの保存領域には置かない。2026-11-22)。

import { type FormEvent, useEffect, useReducer, useRef, useState } from "react";
import type { Api, Character, ConversationTurn, ProactiveMessage } from "./api";
import { type Bubble, type ChatState, apply, initialChat, proactive, restore, sent } from "./chatState";
import { recall, remember } from "./remember";

type Action =
  | { type: "sent"; characterId: string; text: string }
  | { type: "event"; characterId: string; event: string; data: unknown }
  | { type: "proactive"; characterId: string; message: ProactiveMessage }
  | { type: "restore"; characterId: string; sessionId: string; turns: ConversationTurn[] };

type Chats = Record<string, ChatState>;

function reducer(chats: Chats, action: Action): Chats {
  const state = chats[action.characterId] ?? initialChat;
  let next: ChatState;
  if (action.type === "sent") next = sent(state, action.text);
  else if (action.type === "proactive") {
    const { text, session_id: sessionId, turn_no: turnNo } = action.message;
    next = proactive(state, { text, sessionId, turnNo });
  } else if (action.type === "restore") {
    // まだ何も表示していないときだけ、読み直した会話で置き換える(送信中の吹き出しを消さない)
    if (state.bubbles.length > 0) return chats;
    next = restore(action.sessionId, action.turns);
  } else next = apply(state, { event: action.event, data: action.data });
  return { ...chats, [action.characterId]: next };
}

type Props = { api: Api };

export function Chat({ api }: Props) {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [characterId, setCharacterIdState] = useState("");
  const [chats, dispatch] = useReducer(reducer, {});
  const state = chats[characterId] ?? initialChat;
  const [text, setText] = useState("");
  const [loadError, setLoadError] = useState("");
  // キャラクタから話しかけたメッセージ(まだ表示していないもの)。キャラクタを選んだときに表示して、既読にする
  const [pending, setPending] = useState<Record<string, ProactiveMessage> | null>(null);
  // 会話の最後の目印と、下に固定した入力欄。新しい吹き出しが出たら、目印が入力欄のすぐ上に来るまで送る
  const endRef = useRef<HTMLDivElement>(null);
  const inputAreaRef = useRef<HTMLFormElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const setCharacterId = (id: string) => {
    setCharacterIdState(id);
    remember("character", id); // 再表示のときに、同じキャラクタを開く
  };

  useEffect(() => {
    Promise.all([api.characters(), api.proactiveMessages().catch(() => ({ messages: [] as ProactiveMessage[] }))]).then(
      ([list, { messages }]) => {
        setCharacters(list);
        setPending(Object.fromEntries(messages.map((m) => [m.character_id, m])));
        // 話しかけてきたキャラクタがいれば、そのキャラクタを先に開く。なければ、前に開いていたキャラクタ
        const first = messages.find((m) => list.some((c) => c.character_id === m.character_id))?.character_id;
        const saved = recall("character");
        const previous = list.some((c) => c.character_id === saved) ? saved : null;
        if (list.length > 0) setCharacterIdState(first ?? previous ?? list[0].character_id);
      },
      (e: Error) => setLoadError(e.message),
    );
  }, [api]);

  // 表示した自発発話(キャラクタとセッション)。同じメッセージを2回出さない(取り直しと既読の送信が行き違っても)
  const shown = useRef(new Set<string>());
  const key = (m: ProactiveMessage) => `${m.character_id}:${m.session_id}`;
  const seen = (id: string, message: ProactiveMessage) => {
    shown.current.add(key(message));
    api.markProactiveSeen(id, message.session_id).catch(() => undefined);
    setPending((all) => {
      const rest = { ...(all ?? {}) };
      delete rest[id];
      return rest;
    });
  };

  // キャラクタを初めて開いたとき: いまの会話の続きを読み直し、まだ見ていない自発発話を表示する(1回だけ)
  const opened = useRef(new Set<string>());
  // 開いているキャラクタに、あとから届いた自発発話: 応答の途中でなければ、すぐ表示する
  const settled = useRef(new Set<string>());
  useEffect(() => {
    if (!characterId || pending === null) return;
    const id = characterId;
    const message = pending[id];
    if (!opened.current.has(id)) {
      opened.current.add(id);
      (async () => {
        const current = await api.conversation(id).catch(() => null);
        if (current?.session_id && current.turns.length > 0) {
          dispatch({ type: "restore", characterId: id, sessionId: current.session_id, turns: current.turns });
        }
        settled.current.add(id);
        if (message && !shown.current.has(key(message))) {
          // 読み直した会話に、そのメッセージが入っていなければ、表示する
          if (current?.session_id !== message.session_id) dispatch({ type: "proactive", characterId: id, message });
          seen(id, message);
        }
      })();
      return;
    }
    if (!message || !settled.current.has(id) || state.busy) return;
    if (!shown.current.has(key(message))) dispatch({ type: "proactive", characterId: id, message });
    seen(id, message);
    // seen は描画ごとに作り直すが、中身は api と setPending だけなので、依存に入れない
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, characterId, pending, state.busy]);

  // 画面を開いたまま届いた自発発話を取りに行く: 画面に戻ったとき・通知を受け取ったとき(Service Worker から知らせる)
  useEffect(() => {
    let running = false;
    const check = () => {
      if (running || document.visibilityState !== "visible") return;
      running = true;
      api
        .proactiveMessages()
        .then(({ messages }) => {
          const fresh = messages.filter((m) => !shown.current.has(key(m)));
          if (fresh.length > 0) setPending((all) => ({ ...(all ?? {}), ...Object.fromEntries(fresh.map((m) => [m.character_id, m])) }));
        })
        .catch(() => undefined)
        .finally(() => {
          running = false;
        });
    };
    const onMessage = (event: MessageEvent) => {
      if ((event.data as { type?: string } | null)?.type === "mycompanion-message") check();
    };
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);
    navigator.serviceWorker?.addEventListener("message", onMessage);
    return () => {
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("focus", check);
      navigator.serviceWorker?.removeEventListener("message", onMessage);
    };
  }, [api]);

  useEffect(() => {
    // 値を返さない(新しいブラウザの scrollIntoView は Promise を返すので、後片付けの関数と取り違えられる)
    // 入力欄の高さだけ、下に余白をとる(入力欄は固定なので、それ自体を見えるようにしても送られない。高さは入力欄の大きさで変わる)
    const end = endRef.current;
    if (!end) return;
    end.style.scrollMarginBottom = `${inputAreaRef.current?.offsetHeight ?? 0}px`;
    void end.scrollIntoView({ block: "end" });
  }, [state.bubbles]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    // 入力欄に実際にある文字を読む(日本語入力の変換中などで、部品の状態とずれることがあるため)
    const message = (inputRef.current?.value ?? text).trim();
    if (!message || state.busy || !characterId) return;
    const id = characterId; // 応答は、送ったときのキャラクタの会話に入れる
    setText("");
    dispatch({ type: "sent", characterId: id, text: message });
    try {
      await api.streamTurn(id, message, (e) => dispatch({ type: "event", characterId: id, event: e.event, data: e.data }));
    } catch (e) {
      dispatch({ type: "event", characterId: id, event: "error", data: { message: (e as Error).message } });
    }
  }

  const name = characters.find((c) => c.character_id === characterId)?.name ?? "";

  return (
    <section className="chat" aria-label="会話">
      <header className="chat-header page-bar">
        <label>
          キャラクタ{" "}
          <select
            value={characterId}
            disabled={state.busy}
            onChange={(e) => setCharacterId(e.target.value)}
          >
            {characters.map((c) => (
              <option key={c.character_id} value={c.character_id}>
                {pending?.[c.character_id] ? `${c.name}(新しいメッセージ)` : c.name}
              </option>
            ))}
          </select>
        </label>
        <span className="chat-note">{name} は AI です。</span>
      </header>
      {loadError && <p role="alert">{loadError}</p>}
      <div className="chat-log" aria-live="polite">
        {state.bubbles.map((bubble) => (
          <BubbleView key={bubble.id} bubble={bubble} api={api} characterId={characterId} name={name} />
        ))}
        <div ref={endRef} />
      </div>
      <form className="chat-input" ref={inputAreaRef} onSubmit={submit}>
        <label className="visually-hidden" htmlFor="message">
          メッセージ
        </label>
        <textarea
          id="message"
          ref={inputRef}
          value={text}
          rows={2}
          maxLength={2000}
          placeholder="メッセージを入力(Ctrl+Enter で送信)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(e);
          }}
        />
        <button type="submit" disabled={state.busy || !text.trim()}>
          送信
        </button>
      </form>
    </section>
  );
}

function BubbleView({ bubble, api, characterId, name }: { bubble: Bubble; api: Api; characterId: string; name: string }) {
  if (bubble.kind === "user") {
    return (
      <div className="bubble user">
        <p>{bubble.text}</p>
        {bubble.read && <span className="read">既読</span>}
      </div>
    );
  }
  if (bubble.kind === "notice") {
    return (
      <div className="notice" role="note">
        {bubble.text.split("\n").map((line, i) => (
          <p key={i}>{line}</p>
        ))}
      </div>
    );
  }
  if (bubble.kind === "error") {
    return (
      <p className="error" role="alert">
        {bubble.text}
      </p>
    );
  }
  const silent = bubble.variant === "silence";
  return (
    <div className="bubble character">
      <span className="speaker">
        {name}
        {bubble.proactive && <span className="proactive-label">から話しかけてきました</span>}
      </span>
      {bubble.status === "typing" ? (
        <p className="typing" aria-label="入力中">
          …
        </p>
      ) : silent ? (
        // 無言: 入力中の表示のあとに何も出さない(§10.2 の Web の行)。読み上げには、無言だったことを伝える
        <p className="silence" aria-label="(無言)" />
      ) : (
        <p>{bubble.text}</p>
      )}
      {bubble.status === "final" && bubble.sessionId && bubble.turnNo ? (
        <Feedback api={api} characterId={characterId} sessionId={bubble.sessionId} turnNo={bubble.turnNo} />
      ) : null}
    </div>
  );
}

/** 応答ごとの評価(1〜5)と報告(§13.5.8、§14.1)。 */
function Feedback({ api, characterId, sessionId, turnNo }: { api: Api; characterId: string; sessionId: string; turnNo: number }) {
  const [done, setDone] = useState("");
  const target = { character_id: characterId, session_id: sessionId, turn_no: turnNo };

  async function rate(overall: number) {
    try {
      await api.rate({ ...target, overall });
      setDone(`評価しました(${overall})`);
    } catch (e) {
      setDone((e as Error).message);
    }
  }

  async function report() {
    const note = window.prompt("気になった点を、短く書いてください(任意。会話の文章は写さないでください)") ?? "";
    try {
      await api.report({ ...target, reason: "inappropriate", note: note.slice(0, 500) });
      setDone("報告しました。ありがとうございます。");
    } catch (e) {
      setDone((e as Error).message);
    }
  }

  return (
    <div className="feedback" aria-label="この応答の評価と報告">
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} className="rate" onClick={() => rate(n)} aria-label={`この応答を ${n} で評価する`}>
          {n}
        </button>
      ))}
      <button className="report" onClick={report}>
        報告
      </button>
      {done && <span className="feedback-done">{done}</span>}
    </div>
  );
}
