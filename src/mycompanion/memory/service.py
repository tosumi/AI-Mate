"""記憶の管理(本体仕様書 §9)。

- 検索(三層 + ワーキングセット。§9.3)、想起の記録と活性度(§9.9)、再活性化(§9.9.4)
- 短期記憶への書き込み(§9.4.1)、日次整理(§9.4.2)、昇格と月次整理(§9.5)、中期の忘却
- 嗜好度の付与(§9.10)、削除とその伝播(§9.7、§11.5.5)

すべての処理は `(user_id, character_id)` の中で完結する。別のキャラクタの記憶は使わない(§13.4.6)。
整理に失敗しても、会話には影響しない。失敗した日は、短期記憶を残して、次回に再試行する(§9.8)。
"""

from __future__ import annotations

import calendar
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from mycompanion.config import EngineSettings
from mycompanion.llm.client import ConsolidatedMemory, LLMClient, MemoryCandidate, Persona
from mycompanion.memory import association, scoring, topic
from mycompanion.memory.models import CORE_KEYS, CORE_SINGLE, Memory, RecallEvent, Retrieved, RunRecord
from mycompanion.memory.store import MemoryStore
from mycompanion.memory.topic_store import TopicStore
from mycompanion.memory.topics import MovedMemory, Timeline, TopicManager, TopicOffer
from mycompanion.safety import screening

logger = logging.getLogger(__name__)

EXISTING_PER_SHORT = 3  # 照合のために、短期記憶1件につき取り出す既存の記憶の数
EXISTING_LIMIT = 15
PROMOTION_MIN_AGE_DAYS = 7  # 「よく想起される」での昇格に必要な、作成からの日数
CORE_BUDGET = 8  # 「大切なこと」(好みを除く)を、毎回のプロンプトに入れる件数の上限(新しいものを優先)
CORE_PREFERENCE_BUDGET = 6  # 好きなもの・嫌いなものは数が増えるので、いまの話に近いものから、この件数まで
CORE_IMPORTANCE = 0.9
CORE_SAME_SIMILARITY = 0.85  # これ以上似ている「大切なこと」は、同じものとして置き換える
SEARCH_KINDS = ("episodic", "semantic", "relationship")  # 検索で使う種類(接し方の希望は、別に常に読む)


class TurnLog(Protocol):
    """会話の原文(`turns`)への、記憶の管理が使う操作。"""

    async def session_turns(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """セッションのターン(古い順)。各項目は turn_id / user_input / reply_text / response_mode。"""
        ...

    async def delete_turns(self, user_id: uuid.UUID, character_id: str, turn_ids: list[str]) -> int:
        """指定のターンと、それに紐づく感情の推定を削除する(記憶の削除の伝播。§9.7)。"""
        ...


@dataclass
class Retrieval:
    """1ターンの検索結果。番号(m1、t1 …)で下書きに渡し、使われた番号から想起を記録する。"""

    items: dict[str, Retrieved] = field(default_factory=dict)
    query: list[float] | None = None
    timeline: Timeline | None = None  # 話題に合うトピックの経過(§9.13。1件まで)
    offers: dict[str, TopicOffer] = field(default_factory=dict)  # キャラクタが出す話題の候補(トピック。番号は k1)

    def memories(self) -> list[tuple[str, str, bool]]:
        return [
            (handle, _prompt_text(item.memory), item.memory.trust_level == "low")
            for handle, item in self.items.items()
            if item.slot != "topic"
        ]

    def topics(self) -> list[tuple[str, str]]:
        memories = [(h, _prompt_text(item.memory)) for h, item in self.items.items() if item.slot == "topic"]
        return [*memories, *((handle, offer.text) for handle, offer in self.offers.items())]

    def timeline_block(self) -> tuple[str, str, list[str]] | None:
        return self.timeline.block() if self.timeline else None

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "handle": h,
                "slot": i.slot,
                "tier": i.memory.tier,
                "score": round(i.score, 3),
                "id": str(i.memory.memory_id),
                "text": i.memory.summary[:40],  # デバッグ表示用(本人の記憶の冒頭)
            }
            for h, i in self.items.items()
        ]


def _prompt_text(memory: Memory) -> str:
    when = f"({memory.period}の概要)" if memory.kind == "monthly_summary" else ""
    return f"{when}{memory.summary}"


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def previous_month(day: date) -> str:
    first = day.replace(day=1)
    return _month_of(first - timedelta(days=1))


class MemoryManager:
    def __init__(
        self,
        store: MemoryStore,
        llm: LLMClient,
        settings: EngineSettings,
        log: TurnLog | None = None,
        topic_store: TopicStore | None = None,
    ) -> None:
        self._store = store
        # トピック別の時系列記憶(§9.13)。保存先がなければ、使わない
        self.topics: TopicManager | None = (
            TopicManager(topic_store, store, llm, settings)
            if topic_store is not None and settings.topic_memory_enabled
            else None
        )
        self._llm = llm
        self._settings = settings
        self._log = log
        self._preference_cache: dict[tuple[str, int], list[tuple[list[float], float]]] = {}

    @property
    def store(self) -> MemoryStore:
        return self._store

    # ------------------------------------------------------------------
    # 嗜好度(§9.10)と埋め込み
    # ------------------------------------------------------------------

    async def _preference_vectors(self, persona: Persona) -> list[tuple[list[float], float]]:
        key = (persona.character_id, persona.version)
        if key not in self._preference_cache:
            items = [p for p in persona.preferences if p.get("topic")]
            vectors = await self._llm.embed([str(p["topic"]) for p in items]) if items else []
            self._preference_cache[key] = [
                (vector, float(p.get("affinity", 0.0))) for vector, p in zip(vectors, items, strict=True)
            ]
        return self._preference_cache[key]

    async def _embed_with_preference(
        self, persona: Persona, summaries: list[str], topics: list[list[str]]
    ) -> list[tuple[list[float], float]]:
        """要約の埋め込みと、嗜好度をまとめて計算する(埋め込みの呼び出しは1回)。"""
        flat = [topic for group in topics for topic in group]
        vectors = await self._llm.embed([*summaries, *flat]) if summaries else []
        summary_vectors, topic_vectors = vectors[: len(summaries)], vectors[len(summaries) :]
        preferences = await self._preference_vectors(persona)
        result, offset = [], 0
        for index, group in enumerate(topics):
            own = topic_vectors[offset : offset + len(group)]
            offset += len(group)
            # 話題タグがなければ、要約そのものと照合する
            score = scoring.preference_score(own or [summary_vectors[index]], preferences) if preferences else 0.0
            result.append((summary_vectors[index], round(score, 4)))
        return result

    # ------------------------------------------------------------------
    # 検索(§9.3)
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        text: str,
        turn_no: int,
        user_vad: dict[str, float] | None,
        now: datetime,
        timezone: str = "Asia/Tokyo",
    ) -> Retrieval:
        settings = self._settings
        today = scoring.local_day(now, timezone, settings.memory_consolidation_time)
        query = (await self._llm.embed([text]))[0]
        chosen: list[Retrieved] = []

        async def search(tier: str, limit: int, budget: int, slot: str, exclude: set[uuid.UUID]) -> None:
            found = []
            for memory, similarity in await self._store.candidates(user_id, character_id, query, tier, limit):
                if (
                    memory.kind in SEARCH_KINDS
                    and memory.memory_id not in exclude
                    and similarity >= scoring.MIN_SIMILARITY
                ):
                    score = scoring.retrieval_score(memory, similarity, today, user_vad, settings)
                    found.append(Retrieved(memory, slot, score, similarity))
            found.sort(key=lambda item: item.score, reverse=True)
            chosen.extend(found[:budget])

        # 長期: 重要度の高い事実を常に含める(固定枠)。直近の月次の概要も1件
        longs = [m for m in await self._store.list(user_id, character_id, tier="long") if m.searchable]
        # 「大切なこと」(呼び名など)は、話題や時期に関係なく、常に含める
        core = sorted(
            (m for m in longs if m.core_key and m.core_key != "preference"),
            key=lambda m: m.created_at or now,
            reverse=True,
        )[:CORE_BUDGET]
        likes = sorted(
            (m for m in longs if m.core_key == "preference"),
            key=lambda m: scoring.cosine(query, m.embedding),
            reverse=True,
        )[:CORE_PREFERENCE_BUDGET]
        core += likes
        chosen.extend(Retrieved(m, "core", 1.0) for m in core)
        fixed = sorted(
            (m for m in longs if m.kind in SEARCH_KINDS and not m.core_key),
            key=lambda m: (m.pinned, m.importance),
            reverse=True,
        )[: scoring.BUDGET_LONG_FIXED]
        chosen.extend(Retrieved(m, "long_fixed", m.importance) for m in fixed)
        summaries = sorted(
            (m for m in longs if m.kind == "monthly_summary" and not m.stale and m.period), key=lambda m: m.period or ""
        )
        if summaries:
            chosen.append(Retrieved(summaries[-1], "long_summary", 1.0))

        # 短期(当日分): 量が少ないので、常に参照する。「さっき何の話だったっけ」のように、発話と記憶の言葉が
        # 似ていなくても、直前の会話を思い出せるようにする。上限を超えるときだけ、意味の近さと新しさで絞る
        shorts = [
            (memory, similarity)
            for memory, similarity in await self._store.candidates(user_id, character_id, query, "short", 20)
            if memory.kind in SEARCH_KINDS
        ]
        newest = max((m.created_at for m, _ in shorts if m.created_at), default=now)

        def short_score(pair: tuple[Memory, float]) -> float:
            memory, similarity = pair
            age_hours = (newest - memory.created_at).total_seconds() / 3600 if memory.created_at else 24.0
            return max(similarity, 0.0) + 0.3 * 2.0 ** (-age_hours / 6.0) + 0.2 * memory.importance

        shorts.sort(key=short_score, reverse=True)
        chosen.extend(Retrieved(m, "short", short_score((m, sim)), sim) for m, sim in shorts[: scoring.BUDGET_SHORT])
        await search("medium", 30, scoring.BUDGET_MEDIUM, "medium", set())
        await search(
            "long", 20, scoring.BUDGET_LONG_SEARCH, "long_search", {m.memory_id for m in (*fixed, *core)}
        )

        # ワーキングセット: 検索結果に関わらず、プロンプトに含める(§9.9.3)
        working = await self._store.working_set(user_id, character_id, session_id)
        expired = [w.memory_id for w in working if turn_no - w.last_recalled_turn > settings.working_set_ttl_turns]
        if expired:
            await self._store.remove_working(user_id, character_id, session_id, expired)
        alive = sorted(
            (w for w in working if w.memory_id not in expired), key=lambda w: w.last_recalled_turn, reverse=True
        )
        for item in alive[: settings.working_set_size]:
            memory = await self._store.get(user_id, character_id, item.memory_id)
            if memory is not None and memory.searchable:
                chosen.append(Retrieved(memory, "working_set", 0.0))

        # 重複を除く。再活性化のコピーと原本の両方があれば、コピーだけを残す
        copies = {r.memory.origin_memory_id for r in chosen if r.memory.is_copy}
        retrieval = Retrieval(query=query)
        seen: set[uuid.UUID] = set()
        # トピックの枠: 話題に合う、確立したトピックの経過を1件。失敗しても、検索は続ける
        covered: set[uuid.UUID] = set()
        if self.topics is not None:
            try:
                # 記憶からたどるのは、ワーキングセット(この会話で、実際に使った記憶)だけ。検索に掛かっただけの
                # 記憶からたどると、関係のない発話(あいさつなど)でも経過が渡り、キャラクタが自分から話題にしてしまう
                found_ids = {r.memory.memory_id for r in chosen if r.slot == "working_set"}
                retrieval.timeline = await self.topics.timeline_for(user_id, character_id, text, query, found_ids)
            except Exception as exc:  # noqa: BLE001
                logger.warning("トピックの検索に失敗しました: %s", type(exc).__name__)
            if retrieval.timeline is not None:
                covered = retrieval.timeline.source_memory_ids
        for item in chosen:
            if item.memory.memory_id in seen or item.memory.memory_id in copies:
                continue
            # タイムラインと同じ記憶が根拠なら、タイムラインを優先する(同じ話を、二重に渡さない)
            if item.memory.memory_id in covered and item.slot in ("medium", "long_search"):
                continue
            seen.add(item.memory.memory_id)
            retrieval.items[f"m{len(retrieval.items) + 1}"] = item
        return retrieval

    async def select_topic(
        self,
        user_id: uuid.UUID,
        character_id: str,
        retrieval: Retrieval,
        user_vad: dict[str, float] | None,
        now: datetime,
        seed: int,
        timezone: str = "Asia/Tokyo",
        under_18: bool = False,
        session_id: uuid.UUID | None = None,
    ) -> bool:
        """キャラクタが振る話題の候補を選び、検索結果に加える(§8.7)。選べなければ False。

        `session_id` を渡すと、確立したトピック(予定の「その後」を含む)も候補にする。記憶の候補と比べて、
        スコアの高いほうを1件だけ出す。
        """
        today = scoring.local_day(now, timezone, self._settings.memory_consolidation_time)
        already = {item.memory.memory_id for item in retrieval.items.values()}
        pool = [
            m
            for tier in ("short", "medium", "long")
            for m in await self._store.list(user_id, character_id, tier=tier)
            if m.memory_id not in already
        ]
        chosen = topic.select(pool, retrieval.query, now, today, user_vad, self._settings, seed, under_18)
        if self.topics is not None and session_id is not None:
            current = {retrieval.timeline.doc_id} if retrieval.timeline else set()
            offer = await self.topics.offer(
                user_id, character_id, retrieval.query, now, session_id, under_18, current
            )
            if offer is not None and (not chosen or offer.score >= chosen[0][1]):
                retrieval.offers["k1"] = offer
                return True
        for number, (memory, score) in enumerate(chosen, start=1):
            retrieval.items[f"t{number}"] = Retrieved(memory, "topic", score)
        return bool(chosen)

    # ------------------------------------------------------------------
    # 想起の記録、活性度、ワーキングセット(§9.9)
    # ------------------------------------------------------------------

    async def associate(
        self,
        user_id: uuid.UUID,
        character_id: str,
        retrieval: Retrieval,
        focus_id: uuid.UUID,
        persona: Persona,
        visited: set[uuid.UUID],
        now: datetime,
        under_18: bool = False,
    ) -> bool:
        """焦点のトピックから、関係をたどって、次の話題の候補を検索結果に加える(§9.13)。選べなければ False。"""
        if self.topics is None:
            return False
        values = association.params(persona.axes, persona.traits)
        offer = await self.topics.associate(
            user_id, character_id, focus_id, persona.axes, values, visited, now, under_18
        )
        if offer is None:
            return False
        retrieval.offers["k1"] = offer
        return True

    async def proactive_topic(
        self,
        user_id: uuid.UUID,
        character_id: str,
        now: datetime,
        seed: int,
        *,
        timezone: str = "Asia/Tokyo",
        under_18: bool = False,
        general: bool = True,
    ) -> Retrieval | None:
        """自発発話の話題を1つ選ぶ(§15.1 の M8)。なければ None(話題がなければ、話しかけない)。

        優先: ① 予定の「その後」、② ふつうの話題(記憶とトピック。類似による連想を含む)、③ 負の懸念の「その後」
        (2026-11-21 のユーザーの決定。つらい話は、ほかに話題がないときだけ)。
        `general` が False(自発性の低いキャラクタ)なら、① だけ。
        """
        settings = self._settings
        retrieval = Retrieval()
        session_id = uuid.uuid4()  # まだ始まっていないセッション(このセッションでの持ち出しの回数は 0)
        today = scoring.local_day(now, timezone, settings.memory_consolidation_time)
        base = None
        if self.topics is not None:
            base = await self.topics.offer(user_id, character_id, None, now, session_id, under_18)
            if base is not None and base.kind == "followup":
                retrieval.offers["k1"] = base
                return retrieval
        if not general:
            return None
        # ② ふつうの話題: トピック(類似による連想を含む)と記憶の候補を比べて、スコアの高いほうを1件だけ出す
        if self.topics is not None:
            similar = await self.topics.similar_offer(user_id, character_id, now, under_18) if (
                settings.proactive_similar_enabled
            ) else None
            offers = [o for o in (base, similar) if o is not None]
            if offers:
                retrieval.offers["k1"] = max(offers, key=lambda o: o.score)
        pool = [m for tier in ("short", "medium", "long") for m in await self._store.list(user_id, character_id, tier=tier)]
        # いまの会話がないので、関連度の代わりに、記憶の重要度で選ぶ(2026-11-22 のユーザーの決定)
        chosen = topic.select(pool, None, now, today, None, settings, seed, under_18, use_importance=True)
        if chosen and ("k1" not in retrieval.offers or chosen[0][1] > retrieval.offers["k1"].score):
            retrieval.offers.clear()
            retrieval.items["t1"] = Retrieved(chosen[0][0], "topic", chosen[0][1])
        if retrieval.offers or retrieval.items:
            return retrieval
        # ③ 負の懸念の「その後」(ほかに話題がないときだけ)
        if self.topics is not None and settings.proactive_concern_enabled:
            concern = await self.topics.concern_offer(user_id, character_id, now, today, under_18)
            if concern is not None:
                retrieval.offers["k1"] = concern
                return retrieval
        return None

    async def character_mentions(self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID) -> int:
        """このセッションで、キャラクタが自分から話題を持ち出した回数(記憶とトピックの合計)。"""
        count = await self._store.count_character_recalls(user_id, character_id, session_id)
        if self.topics is not None:
            count += await self.topics.character_mentions(user_id, character_id, session_id)
        return count

    async def refresh_activation(
        self, user_id: uuid.UUID, character_id: str, memory: Memory, today: date, now: datetime
    ) -> float:
        events = await self._store.recalls(user_id, character_id, memory.memory_id)
        value = round(scoring.activation(events, today, memory.preference, self._settings), 4)
        days, user_days = scoring.recall_day_counts(events)
        last = max((e.ts for e in events if e.status == "confirmed" and e.ts), default=memory.last_recalled_at)
        await self._store.update(
            user_id, character_id, memory.memory_id,
            activation=value, recall_days=days, user_recall_days=user_days, last_recalled_at=last,
        )  # fmt: skip
        return value

    async def record_usage(
        self,
        user_id: uuid.UUID,
        character_id: str,
        session_id: uuid.UUID,
        turn_id: str,
        turn_no: int,
        retrieval: Retrieval,
        used_handles: list[str],
        now: datetime,
        timezone: str = "Asia/Tokyo",
    ) -> list[str]:
        """応答で実際に使われた記憶を、想起として記録する。検索でヒットしただけのものは、数えない。"""
        settings = self._settings
        today = scoring.local_day(now, timezone, settings.memory_consolidation_time)
        used_ids: list[str] = []
        if self.topics is not None:
            # トピックの想起は、記憶の想起とは別に数える(根拠の記憶の期限は、延びない)
            timeline = retrieval.timeline
            if timeline is not None and timeline.matched_by in ("name", "similarity"):
                await self.topics.record_user_recall(
                    user_id, character_id, timeline.doc_id, session_id, turn_id, today, now
                )
            for handle in dict.fromkeys(used_handles):
                if handle in retrieval.offers:
                    await self.topics.record_offer_used(
                        user_id, character_id, retrieval.offers[handle], session_id, turn_id, today, now
                    )
        for handle in dict.fromkeys(used_handles):
            item = retrieval.items.get(handle)
            if item is None:
                continue
            memory = item.memory
            # コピーが使われたら、原本の想起として数える(層は変えない)
            target_id = memory.origin_memory_id or memory.memory_id
            used_ids.append(str(memory.memory_id))
            await self._store.put_working(
                user_id, character_id, session_id, memory.memory_id, turn_no, settings.working_set_ttl_turns
            )
            if item.slot in ("long_fixed", "core") and item.similarity < scoring.MIN_SIMILARITY:
                continue  # 固定枠に入っているだけの記憶は、想起とみなさない(§9.9.1)
            character = item.slot == "topic"
            pending = character and settings.recall_character_requires_engagement
            event = RecallEvent(
                memory_id=target_id,
                day=today,
                trigger="character" if character else "user",
                source="topic_selection" if character else ("working_set" if item.slot == "working_set" else "search"),
                weight=settings.recall_weight_character if character else settings.recall_weight_user,
                status="pending" if pending else "confirmed",
                ts=now,
                session_id=session_id,
                turn_id=turn_id,
            )
            await self._store.add_recall(user_id, character_id, event)
            if character:
                await self._store.update(user_id, character_id, target_id, last_mentioned_by_character_at=now)
            target = await self._store.get(user_id, character_id, target_id)
            if target is not None and not pending:
                await self.refresh_activation(user_id, character_id, target, today, now)
        return used_ids

    async def resolve_pending(
        self,
        user_id: uuid.UUID,
        character_id: str,
        query: list[float] | None,
        valence: float,
        now: datetime,
        timezone: str = "Asia/Tokyo",
    ) -> dict[str, int]:
        """キャラクタ起点の想起を、次のユーザー発話で確定または破棄する(§9.9.1)。

        確定: ユーザーがその話題に触れ(発話と記憶が意味的に近い)、感情が急に悪化していない。
        無視された、話題を変えられた、ネガティブな反応があった場合は、破棄して数えない。
        """
        today = scoring.local_day(now, timezone, self._settings.memory_consolidation_time)
        result = {"confirmed": 0, "discarded": 0}
        if self.topics is not None:
            for key, value in (
                await self.topics.resolve_pending(user_id, character_id, query, valence, today, now)
            ).items():
                result[key] += value
        for event in await self._store.pending_recalls(user_id, character_id):
            memory = await self._store.get(user_id, character_id, event.memory_id)
            engaged = (
                memory is not None
                and valence > -0.5
                and scoring.cosine(query, memory.embedding) >= scoring.MIN_SIMILARITY
            )
            status = "confirmed" if engaged else "discarded"
            await self._store.set_recall_status(user_id, character_id, event, status, now)
            result[status] += 1
            if engaged and memory is not None:
                await self.refresh_activation(user_id, character_id, memory, today, now)
        return result

    # ------------------------------------------------------------------
    # 短期記憶への書き込み(§9.4.1)
    # ------------------------------------------------------------------

    async def write_session(
        self,
        user_id: uuid.UUID,
        character_id: str,
        persona: Persona,
        session_id: uuid.UUID,
        now: datetime,
        timezone: str = "Asia/Tokyo",
    ) -> int:
        """セッションの会話から、記憶の候補を作って、短期記憶に書く。書いた件数を返す。"""
        await self._store.remove_working(user_id, character_id, session_id)  # セッションが終わると、クリアする
        if self._log is None:
            return 0
        turns = await self._log.session_turns(user_id, character_id, session_id)
        if not turns:
            return 0
        turn_ids = {str(t["turn_id"]) for t in turns}
        existing = await self._store.list(user_id, character_id)
        if any(turn_ids & set(m.source_event_ids) for m in existing):
            return 0  # このセッションは、すでに書き込み済み(冪等)

        lines: list[tuple[int, str, str]] = []
        owner: dict[int, str] = {}
        for turn in turns:
            # システム通知だけのターンと、重大な検出でブロックした入力は、記憶にしない(§13.5.6)
            if turn.get("response_mode") == "notice" or "input_blocked" in (turn.get("safety_flags") or []):
                continue
            proactive = turn.get("response_mode") == "proactive"  # キャラクタから話しかけた(ユーザーの発話はない)
            if not proactive:
                number = len(lines) + 1
                lines.append((number, "user", str(turn.get("user_input", ""))))
                owner[number] = str(turn["turn_id"])
            if turn.get("reply_text"):
                number = len(lines) + 1
                lines.append((number, "assistant", str(turn["reply_text"])))
                owner[number] = str(turn["turn_id"])
        if not any(role == "user" for _, role, _ in lines):
            return 0  # ユーザーが話していない(返事のなかった自発発話だけ)セッションは、記憶にしない
        result = await self._llm.write_memories(lines)
        candidates = result.candidates
        if not candidates:
            return 0
        day = scoring.local_day(now, timezone, self._settings.memory_consolidation_time)
        computed = await self._embed_with_preference(
            persona, [c.summary for c in candidates], [c.topics for c in candidates]
        )
        for candidate, (vector, preference) in zip(candidates, computed, strict=True):
            sources = sorted({owner[n] for n in candidate.source_turns if n in owner})
            memory = self._new_memory(candidate, "short", day, now, vector, preference, persona.version, sources)
            is_core = candidate.core_key in CORE_KEYS and not memory.quarantined and not memory.sensitivity
            if not is_core:
                await self._store.add(user_id, character_id, memory)
                continue
            # 「大切なこと」: 長期に直接置く。日次整理・月次整理・忘却の対象にしない(消せるのは、ユーザーだけ)
            memory.tier, memory.core_key, memory.pinned = "long", candidate.core_key, True
            memory.kind = "semantic" if candidate.kind == "episodic" else candidate.kind
            memory.importance = max(memory.importance, CORE_IMPORTANCE)
            previous = [
                m for m in existing
                if m.core_key == candidate.core_key
                and (candidate.core_key in CORE_SINGLE or scoring.cosine(m.embedding, vector) >= CORE_SAME_SIMILARITY)
            ]  # fmt: skip
            if previous:
                # 同じこと(呼び名は1件だけ)は、増やさずに、新しい内容に置き換える
                old = previous[0]
                await self._store.update(
                    user_id, character_id, old.memory_id,
                    summary=memory.summary, topics=memory.topics, embedding=vector, version=old.version + 1,
                    source_event_ids=sorted({*old.source_event_ids, *sources}),
                )  # fmt: skip
            else:
                await self._store.add(user_id, character_id, memory)
        return len(candidates)

    @staticmethod
    def _new_memory(
        item: MemoryCandidate | ConsolidatedMemory,
        tier: str,
        day: date,
        now: datetime,
        vector: list[float],
        preference: float,
        persona_version: int,
        source_event_ids: list[str],
    ) -> Memory:
        suspicious = screening.is_suspicious_memory(item.summary)
        return Memory(
            memory_id=uuid.uuid4(),
            tier=tier,
            kind=item.kind,
            summary=item.summary,
            topics=list(item.topics),
            emotion_vad={"valence": item.valence, "arousal": item.arousal, "dominance": 0.0},
            importance=item.importance,
            sensitivity=None if item.sensitivity == "none" else item.sensitivity,
            pinned=bool(getattr(item, "pinned", False)),
            # 命令文が混じった記憶は、隔離する(検索の対象から外し、昇格させない。§13.5.6)
            quarantined=suspicious,
            quarantine_reason="instruction_like" if suspicious else None,
            trust_level="low" if suspicious else "normal",
            preference=preference,
            preference_version=persona_version,
            source_day=day,
            source_event_ids=source_event_ids,
            embedding=vector,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # 日次整理(§9.4.2)
    # ------------------------------------------------------------------

    async def consolidate_daily(
        self,
        user_id: uuid.UUID,
        character_id: str,
        persona: Persona,
        day: date,
        now: datetime,
        dry_run: bool = False,
        keep_sensitive: bool = False,
        timezone: str = "Asia/Tokyo",
    ) -> RunRecord:
        """`day` までの短期記憶を整理して、中期記憶に移す。冪等(2回実行しても、重複しない)。

        `keep_sensitive` は、センシティブな内容を記憶に残すことに、ユーザーが同意しているか(18歳以上のみ)。
        """
        settings = self._settings
        run = RunRecord(run_type="daily", target=day.isoformat(), status="success", started_at=now)
        shorts = [
            m for m in await self._store.list(user_id, character_id, tier="short") if m.source_day and m.source_day <= day
        ]
        if not shorts:
            run.status = "skipped"
            return run
        copies = [m for m in shorts if m.is_copy]
        # センシティブ項目は、同意(sensitive_memory)がない限り、中期に移さず削除する(§9.4.1)。
        # ユーザー以外の人のセンシティブな内容(third_party)は、同意があっても残さない
        sensitive = [
            m for m in shorts
            if not m.is_copy and m.sensitivity and (m.sensitivity == "third_party" or not keep_sensitive)
        ]  # fmt: skip
        # 隔離された記憶と、接し方の希望は、LLM に渡さず、そのまま中期に移す
        direct = [
            m for m in shorts
            if not m.is_copy and m not in sensitive and (m.quarantined or m.kind == "interaction_preference")
        ]  # fmt: skip
        inputs = [m for m in shorts if not m.is_copy and m not in sensitive and m not in direct]
        run.counts = {
            "input": len(shorts), "sensitive_dropped": len(sensitive), "moved_directly": len(direct),
            "copies": len(copies), "written": 0, "updated": 0, "promoted": 0, "expired": 0, "reactivated": 0,
        }  # fmt: skip
        if dry_run:
            run.status = "skipped"
            run.counts["dry_run"] = True
            return run

        try:
            expires = now + timedelta(days=settings.memory_medium_retention_days)
            moved: list[MovedMemory] = []  # その日に中期へ移した記憶(トピックの整理に渡す)
            if inputs:
                existing = await self._existing_for(user_id, character_id, inputs, copies)
                result = await self._llm.consolidate_day(
                    [self._describe(m) for m in inputs], [self._describe(m) for m in existing]
                )
                run.model_id, run.prompt_version = result.model_id, result.prompt_version
                items = result.consolidated
                computed = await self._embed_with_preference(
                    persona, [i.summary for i in items], [i.topics for i in items]
                )
                for item, (vector, preference) in zip(items, computed, strict=True):
                    sources = [inputs[n] for n in item.source_ids if 0 <= n < len(inputs)]
                    events = sorted({e for m in sources for e in m.source_event_ids})
                    # 話した時刻: もとの短期記憶が作られた時刻(セッションの終わり)で代える
                    told_at = min((m.created_at for m in sources if m.created_at), default=now)
                    if item.update_of is not None and item.update_of < len(existing):
                        # 既存の記憶と同じ内容・矛盾する内容: 新しい情報で更新する(§9.4.2 の手順4)
                        old = existing[item.update_of]
                        await self._store.update(
                            user_id, character_id, old.memory_id,
                            summary=item.summary, topics=list(item.topics), embedding=vector, preference=preference,
                            importance=max(old.importance, item.importance), version=old.version + 1,
                            source_event_ids=sorted({*old.source_event_ids, *events}),
                        )  # fmt: skip
                        run.counts["updated"] += 1
                        updated = await self._store.get(user_id, character_id, old.memory_id)
                        if updated is not None:
                            moved.append(MovedMemory(updated, told_at))
                        continue
                    memory = self._new_memory(item, "medium", day, now, vector, preference, persona.version, events)
                    memory.pinned = any(m.pinned for m in sources)
                    memory.muted = any(m.muted for m in sources)  # 「持ち出さないで」は、整理しても引き継ぐ
                    memory.expires_at = expires
                    memory.consolidated_at = now
                    await self._store.add(user_id, character_id, memory)
                    run.counts["written"] += 1
                    moved.append(MovedMemory(memory, told_at))
            for memory in direct:
                await self._store.update(
                    user_id, character_id, memory.memory_id, tier="medium", expires_at=expires, consolidated_at=now
                )
            # 再固定化: 再活性化のコピーの原本が中期なら、期限を延長する。コピーは削除する(§9.9.4)
            for copy in copies:
                origin = await self._store.get(user_id, character_id, copy.origin_memory_id)  # type: ignore[arg-type]
                if origin is not None and origin.tier == "medium" and origin.created_at is not None:
                    limit = origin.created_at + timedelta(days=settings.memory_medium_retention_days * 2)
                    await self._store.update(user_id, character_id, origin.memory_id, expires_at=min(expires, limit))
            # 書き込みが済んだ短期記憶を削除する(「移す」)
            await self._store.hard_delete(
                user_id, character_id, [m.memory_id for m in (*inputs, *sensitive, *copies)]
            )

            # 手順4.5: トピックへの割り当てと、タイムラインへの追記(§9.13)。失敗しても、記憶の整理は成功とする
            if self.topics is not None:
                try:
                    run.counts["topics"] = await self.topics.assign_day(
                        user_id, character_id, moved, day, now, timezone, await self._preference_vectors(persona)
                    )
                    run.counts["topics"] |= await self.topics.review(user_id, character_id, now)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("トピックの整理に失敗しました: %s", type(exc).__name__)
                    run.counts["topics_error"] = type(exc).__name__

            run.counts["promoted"] = await self.promote(user_id, character_id, now)
            run.counts["expired"] = await self.expire(user_id, character_id, now)
            await self.update_baseline(user_id, character_id, now)
            run.counts["reactivated"] = await self.reactivate(user_id, character_id, day + timedelta(days=1), now)
        except Exception as exc:  # noqa: BLE001 - 失敗しても会話には影響させない。短期記憶は残り、次回に再試行する
            logger.warning("日次整理に失敗しました: %s", type(exc).__name__)
            run.status, run.error = "failed", type(exc).__name__
        run.finished_at = datetime.now(UTC)
        await self._store.add_run(user_id, character_id, run)
        return run

    @staticmethod
    def _describe(memory: Memory) -> str:
        """整理の LLM に渡す1行。本文の前に、種類・重要度・気持ちを付ける。"""
        valence = (memory.emotion_vad or {}).get("valence", 0.0)
        return (
            f"[kind={memory.kind} importance={memory.importance:.2f} valence={valence:+.2f} "
            f"sensitivity={memory.sensitivity or 'none'}] {memory.summary}"
        )

    async def _existing_for(
        self, user_id: uuid.UUID, character_id: str, inputs: list[Memory], copies: list[Memory]
    ) -> list[Memory]:
        """照合のための既存の記憶(短期記憶に近い中期・長期と、再活性化のコピーの原本)。"""
        found: dict[uuid.UUID, Memory] = {}
        for copy in copies:
            origin = await self._store.get(user_id, character_id, copy.origin_memory_id)  # type: ignore[arg-type]
            if origin is not None and origin.searchable:
                found[origin.memory_id] = origin
        for memory in inputs:
            if not memory.embedding:
                continue
            for tier in ("medium", "long"):
                for candidate, similarity in await self._store.candidates(
                    user_id, character_id, memory.embedding, tier, EXISTING_PER_SHORT
                ):
                    # 「大切なこと」は、整理で書き換えない
                    if similarity >= scoring.MIN_SIMILARITY and candidate.kind in SEARCH_KINDS and not candidate.core_key:
                        found[candidate.memory_id] = candidate
        return list(found.values())[:EXISTING_LIMIT]

    # ------------------------------------------------------------------
    # 昇格、忘却、baseline、再活性化
    # ------------------------------------------------------------------

    async def promote(self, user_id: uuid.UUID, character_id: str, now: datetime) -> int:
        """昇格の条件(§9.5)を満たした中期記憶を、長期に移す。隔離された記憶は、昇格させない。"""
        settings = self._settings
        count = 0
        for memory in await self._store.list(user_id, character_id, tier="medium"):
            if memory.quarantined:
                continue
            age = (now - memory.created_at).days if memory.created_at else 0
            recalled = (
                memory.recall_days >= settings.memory_promotion_min_refs
                and memory.user_recall_days >= 1  # キャラクタが持ち出した話題だけでは、昇格しない(§9.9.5)
                and age >= PROMOTION_MIN_AGE_DAYS
            )
            # 同じ事実・話題が、異なる日に現れるたびに、日次整理で更新されて版が上がる
            repeated = memory.version >= settings.memory_promotion_min_days
            if memory.pinned or memory.importance >= settings.memory_promotion_importance or recalled or repeated:
                await self._store.update(
                    user_id, character_id, memory.memory_id,
                    tier="long", promoted_from=memory.memory_id, promoted_at=now, expires_at=None,
                )  # fmt: skip
                count += 1
        return count

    async def promote_manually(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, now: datetime) -> bool:
        memory = await self._store.get(user_id, character_id, memory_id)
        if memory is None or memory.tier != "medium" or memory.quarantined:
            return False
        await self._store.update(
            user_id, character_id, memory_id, tier="long", promoted_from=memory_id, promoted_at=now, expires_at=None
        )
        return True

    async def expire(self, user_id: uuid.UUID, character_id: str, now: datetime) -> int:
        """期限を過ぎた中期記憶を削除する。その月の月次の概要が作成済みのものに限る(§9.5)。"""
        summarized = {
            m.period for m in await self._store.list(user_id, character_id, kind="monthly_summary") if not m.stale
        }
        expired = [
            m.memory_id
            for m in await self._store.list(user_id, character_id, tier="medium")
            if m.expires_at is not None and m.expires_at < now and m.source_day and _month_of(m.source_day) in summarized
        ]
        if expired and self.topics is not None:
            # 記憶を消す前に、トピックの文書を更新する(候補は項目も消し、確立したものは項目を残す)
            await self.topics.on_memories_expired(user_id, character_id, set(expired), now)
        return await self._store.soft_delete(user_id, character_id, expired, now) if expired else 0

    async def update_baseline(self, user_id: uuid.UUID, character_id: str, now: datetime) -> None:
        """ユーザーの baseline を、このキャラクタの中期・長期の記憶の感情タグから再計算する(§9.6)。"""
        memories = [
            m
            for tier in ("medium", "long")
            for m in await self._store.list(user_id, character_id, tier=tier)
            if m.emotion_vad and m.kind in SEARCH_KINDS and not m.quarantined
        ]
        days = len({m.source_day for m in memories if m.source_day})
        sessions = await self._store.count_sessions(user_id, character_id)
        await self._store.set_baseline(
            user_id, character_id, scoring.average_vad([m.emotion_vad or {} for m in memories]), {}, sessions, days, now
        )

    async def reactivate(self, user_id: uuid.UUID, character_id: str, day: date, now: datetime) -> int:
        """活性度が高い中期・長期の記憶を、短期に参照コピーとして呼び戻す(§9.9.4)。原本は動かさない。"""
        settings = self._settings
        existing_copies = {
            m.origin_memory_id for m in await self._store.list(user_id, character_id, tier="short") if m.is_copy
        }
        ready: list[tuple[float, Memory]] = []
        for tier, threshold in (
            ("medium", settings.reactivation_threshold_medium),
            ("long", settings.reactivation_threshold_long),
        ):
            for memory in await self._store.list(user_id, character_id, tier=tier):
                if memory.kind not in SEARCH_KINDS or memory.quarantined or memory.memory_id in existing_copies:
                    continue
                cooling = memory.last_reactivated_at is not None and (
                    now - memory.last_reactivated_at
                ) < timedelta(days=settings.reactivation_cooldown_days)
                value = await self.refresh_activation(user_id, character_id, memory, day, now)
                if value >= threshold and not cooling:
                    ready.append((value, memory))
        ready.sort(key=lambda pair: pair[0], reverse=True)
        for _, memory in ready[: settings.reactivation_daily_cap]:
            copy = Memory(
                memory_id=uuid.uuid4(), tier="short", kind=memory.kind, summary=memory.summary,
                topics=list(memory.topics), emotion_vad=memory.emotion_vad, importance=memory.importance,
                sensitivity=memory.sensitivity, preference=memory.preference,
                preference_version=memory.preference_version, origin_memory_id=memory.memory_id,
                source_day=day, embedding=memory.embedding, created_at=now,
            )  # fmt: skip
            await self._store.add(user_id, character_id, copy)
            await self._store.update(user_id, character_id, memory.memory_id, last_reactivated_at=now)
        return min(len(ready), settings.reactivation_daily_cap)

    # ------------------------------------------------------------------
    # 月次整理(§9.5)
    # ------------------------------------------------------------------

    async def consolidate_monthly(
        self, user_id: uuid.UUID, character_id: str, period: str, now: datetime, dry_run: bool = False
    ) -> RunRecord:
        """`period`(YYYY-MM)の中期記憶から、月次の概要を作り、長期に追記する。再実行は更新として扱う。"""
        run = RunRecord(run_type="monthly", target=period, status="success", started_at=now)
        in_month = [
            m
            for tier in ("medium", "long")
            for m in await self._store.list(user_id, character_id, tier=tier)
            if m.source_day and _month_of(m.source_day) == period and m.kind in SEARCH_KINDS and m.searchable
            and not m.core_key  # 「大切なこと」は、月ごとの概要にまとめない(時期に関係なく残す)
        ]
        sources = [m for m in in_month if m.tier == "medium"]
        promoted = [m for m in in_month if m.tier == "long"]  # 長期に昇格済みのものは、本文で繰り返さない
        run.counts = {"input": len(sources), "promoted": len(promoted)}
        existing = next(
            (m for m in await self._store.list(user_id, character_id, kind="monthly_summary") if m.period == period),
            None,
        )
        if not sources and not promoted:
            run.status = "skipped"
            if existing is not None and existing.stale:
                # 元の記憶がすべて削除された: 概要も残さない(§9.7)
                await self._store.soft_delete(user_id, character_id, [existing.memory_id], now)
            return run
        if dry_run:
            run.status = "skipped"
            run.counts["dry_run"] = True
            return run
        try:
            vad = scoring.average_vad([m.emotion_vad or {} for m in in_month if m.emotion_vad])
            stats = f"記憶の数: {len(in_month)}。気持ちの平均: 快・不快 {vad['valence']:+.2f}、高ぶり {vad['arousal']:+.2f}。"
            result = await self._llm.summarize_month(
                period, [self._describe(m) for m in sources], [m.summary for m in promoted], stats
            )
            run.model_id, run.prompt_version = result.model_id, result.prompt_version
            vector = (await self._llm.embed([result.summary]))[0]
            year, month = (int(part) for part in period.split("-"))
            last_day = date(year, month, calendar.monthrange(year, month)[1])
            source_ids = [str(m.memory_id) for m in sources]
            if existing is not None:
                await self._store.update(
                    user_id, character_id, existing.memory_id,
                    summary=result.summary, topics=result.topics, embedding=vector, source_memory_ids=source_ids,
                    stale=False, version=existing.version + 1, emotion_vad=vad,
                )  # fmt: skip
                run.counts["updated"] = 1
            else:
                await self._store.add(
                    user_id,
                    character_id,
                    Memory(
                        memory_id=uuid.uuid4(), tier="long", kind="monthly_summary", summary=result.summary,
                        topics=result.topics, emotion_vad=vad, importance=0.6, period=period, source_day=last_day,
                        source_memory_ids=source_ids, embedding=vector, created_at=now,
                    ),
                )
                run.counts["written"] = 1
        except Exception as exc:  # noqa: BLE001 - 失敗しても、中期記憶はそのまま残る。次回に再試行する
            logger.warning("月次整理に失敗しました: %s", type(exc).__name__)
            run.status, run.error = "failed", type(exc).__name__
        run.finished_at = datetime.now(UTC)
        await self._store.add_run(user_id, character_id, run)
        return run

    # ------------------------------------------------------------------
    # 利用者の操作(一覧、削除、非表示、訂正。§9.7、§10.3)
    # ------------------------------------------------------------------

    async def forget(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, now: datetime) -> bool:
        """記憶を削除する。派生物(再活性化のコピー、月次の概要の該当部分、根拠のターン)にも伝播させる。"""
        memory = await self._store.get(user_id, character_id, memory_id)
        if memory is None:
            return False
        everything = await self._store.list(user_id, character_id)
        targets = {memory_id}
        targets |= {m.memory_id for m in everything if m.origin_memory_id == memory_id}  # 再活性化のコピー
        if memory.origin_memory_id:
            targets.add(memory.origin_memory_id)  # コピーを消したら、原本も消す(利用者は同じ記憶として見ている)
        stale_periods = []
        for item in everything:
            if item.kind == "monthly_summary" and {str(t) for t in targets} & set(item.source_memory_ids):
                # 削除された記憶に由来する概要は、作り直すまで使わない
                await self._store.update(user_id, character_id, item.memory_id, stale=True)
                stale_periods.append(item.period)
        events = sorted({e for m in everything if m.memory_id in targets for e in m.source_event_ids})
        await self._store.soft_delete(user_id, character_id, list(targets), now)
        topics_touched = 0
        if self.topics is not None:
            # トピックの項目・出典・改訂履歴、アーカイブの概要にも伝播させる(確立していても消す)
            topics_touched = await self.topics.on_memories_deleted(user_id, character_id, targets, now)
        deleted_turns = 0
        if self._log is not None and events:
            deleted_turns = await self._log.delete_turns(user_id, character_id, events)
        for period in stale_periods:
            if period:
                await self.consolidate_monthly(user_id, character_id, period, now)
        await self._store.record_deletion(
            user_id, f"memory:{memory_id}", "completed",
            {
                "memories": len(targets), "turns": deleted_turns, "summaries_regenerated": len(stale_periods),
                "topics": topics_touched,
            }, now,
        )  # fmt: skip
        return True

    async def set_muted(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, muted: bool) -> bool:
        if await self._store.get(user_id, character_id, memory_id) is None:
            return False
        await self._store.update(user_id, character_id, memory_id, muted=muted)
        return True

    async def edit(
        self, user_id: uuid.UUID, character_id: str, persona: Persona, memory_id: uuid.UUID, summary: str
    ) -> bool:
        """記憶の訂正。埋め込みと嗜好度を計算し直す。"""
        memory = await self._store.get(user_id, character_id, memory_id)
        summary = summary.strip()
        if memory is None or not summary:
            return False
        ((vector, preference),) = await self._embed_with_preference(persona, [summary], [memory.topics])
        suspicious = screening.is_suspicious_memory(summary)
        await self._store.update(
            user_id, character_id, memory_id,
            summary=summary[:400], embedding=vector, preference=preference, version=memory.version + 1,
            quarantined=suspicious, quarantine_reason="instruction_like" if suspicious else None,
        )  # fmt: skip
        return True

    async def status(self, user_id: uuid.UUID, character_id: str) -> dict[str, Any]:
        memories = await self._store.list(user_id, character_id)
        runs = await self._store.runs(user_id, character_id)

        def last(run_type: str) -> dict[str, Any] | None:
            found = [r for r in runs if r.run_type == run_type]
            if not found:
                return None
            run = found[-1]
            return {
                "target": run.target, "status": run.status, "counts": run.counts,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            }  # fmt: skip

        return {
            "counts": {tier: sum(1 for m in memories if m.tier == tier) for tier in ("short", "medium", "long")},
            "last_daily": last("daily"),
            "last_monthly": last("monthly"),
        }
