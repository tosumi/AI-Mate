// 記憶の画面(本体仕様書 §9、§9.12、§9.13、§10.3、§15.1 の M7)。
// 記憶の一覧・話題に出さない(mute)・削除と、トピック(話題ごとの経過)の一覧・mute・アーカイブ。

import { useCallback, useEffect, useState } from "react";
import type { Api, Character, Memory, Topic } from "./api";
import { useRefreshOnOpen } from "./useRefreshOnOpen";

const TIERS: Record<string, string> = { short: "今日", medium: "最近", long: "長く覚えている" };

export function Memories({ api, active = true }: { api: Api; active?: boolean }) {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [characterId, setCharacterId] = useState("");
  const [memories, setMemories] = useState<Memory[] | null>(null);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.characters().then(
      (list) => {
        setCharacters(list);
        if (list.length > 0) setCharacterId(list[0].character_id);
      },
      (e: Error) => setMessage(e.message),
    );
  }, [api]);

  const load = useCallback(async () => {
    if (!characterId) return;
    try {
      const [list, topicList] = await Promise.all([api.memories(characterId), api.topics(characterId)]);
      setMemories(list);
      setTopics(topicList.topics);
    } catch (e) {
      setMessage((e as Error).message);
    }
  }, [api, characterId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 開いたとき: 前の内容を出したまま、裏で新しくする(会話のあとに増えた記憶など)
  useRefreshOnOpen(active, () => void load());

  async function act(run: () => Promise<unknown>, done: string) {
    setMessage("");
    try {
      await run();
      setMessage(done);
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    }
  }

  return (
    <section className="panel" aria-labelledby="memories-title">
      {/* 見出しは読み上げ用に残し、画面には出さない。ページはキャラクタの選択から始める(2026-11-23 のユーザーの指示) */}
      <h1 id="memories-title" className="visually-hidden">記憶</h1>
      <div className="page-bar">
        <label>
          キャラクタ{" "}
          <select value={characterId} onChange={(e) => setCharacterId(e.target.value)}>
            {characters.map((c) => (
              <option key={c.character_id} value={c.character_id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="hint">
        会話から作られた記憶です。キャラクタは、これを手がかりに話します。「話題に出さない」にすると、キャラクタが自分からは持ち出しません。削除すると、元に戻せません。
      </p>
      {message && <p role="status">{message}</p>}

      <h2>話題ごとの経過</h2>
      {topics.length === 0 ? (
        <p className="empty">まだありません。</p>
      ) : (
        <ul className="items">
          {topics.map((topic) => (
            <li key={topic.id}>
              <div>
                <strong>{topic.title}</strong>
                {topic.muted && <span className="badge">話題に出さない</span>}
                <p className="sub">
                  {topic.description}(出来事 {topic.entries}件)
                </p>
              </div>
              <div className="actions">
                <button
                  onClick={() =>
                    act(() => api.setTopicMuted(characterId, topic.id, !topic.muted), topic.muted ? "話題に出してよいことにしました。" : "話題に出さないようにしました。")
                  }
                >
                  {topic.muted ? "話題に出してよい" : "話題に出さない"}
                </button>
                <button
                  onClick={() => {
                    if (window.confirm(`「${topic.title}」の経過を削除しますか。概要だけが残ります。`)) {
                      void act(() => api.archiveTopic(characterId, topic.id), "削除しました(概要だけが残ります)。");
                    }
                  }}
                >
                  削除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <h2>記憶</h2>
      {memories === null ? (
        <p aria-busy="true">読み込んでいます…</p>
      ) : memories.length === 0 ? (
        <p className="empty">まだありません。</p>
      ) : (
        <ul className="items">
          {memories.map((memory) => (
            <li key={memory.id}>
              <div>
                {memory.core && <span aria-label="大切なこと">⭐ </span>}
                {memory.summary}
                <p className="sub">
                  {TIERS[memory.tier] ?? memory.tier}
                  {memory.source_day ? `・${memory.source_day}` : ""}
                  {memory.muted && <span className="badge">話題に出さない</span>}
                </p>
              </div>
              <div className="actions">
                <button
                  onClick={() =>
                    act(() => api.setMemoryMuted(characterId, memory.id, !memory.muted), memory.muted ? "話題に出してよいことにしました。" : "話題に出さないようにしました。")
                  }
                >
                  {memory.muted ? "話題に出してよい" : "話題に出さない"}
                </button>
                <button
                  onClick={() => {
                    if (window.confirm("この記憶を削除しますか。元に戻せません。")) {
                      void act(() => api.forgetMemory(characterId, memory.id), "削除しました。");
                    }
                  }}
                >
                  削除
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
