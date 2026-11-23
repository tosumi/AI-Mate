"""トピック別の時系列記憶(本体仕様書 §9.13。`topic-timeline-memory.md`)。

記憶の3層(いつの記憶か)とは別に、**話題ごとの経過**を持つ。層とは直交する: トピックは層を持たず、
中期・長期の記憶を「根拠」として参照するだけで、記憶を移動させない。中期の記憶の期限も、延ばさない。

- 日次整理の手順4.5: その日に中期へ移す記憶を、トピックに割り当て、タイムラインに項目を追記する。
  LLM は操作を返すだけで、文書はコードが組み立てる(`memory.okf`)。
- 確立(`stable`)したトピックだけを、会話で使う。候補(`draft`)は使わない。
- 検索の枠: 会話の話題に合うトピックを1件、最大5項目、プロンプトに入れる。
- 記憶の削除は、項目にも伝播する(確立していても消す)。期限切れは、確立したトピックの項目を残す。
- トピックの削除は、アーカイブ(概要だけを残す)。アーカイブは、想起の対象にせず、応答にも表出しない。

「大切なこと」(§9.12)とは並存する。あちらは時系列のない事実で、毎回渡す。こちらは経過で、話題に合うときだけ渡す。
ユーザーの性格・傾向は、トピックにしない。キャラクタをまたいで、共有しない。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mycompanion.config import EngineSettings
from mycompanion.llm.client import LLMClient, TopicCandidate, TopicEntryOp, TopicMemoryInput, TopicRelationOp
from mycompanion.memory import association, okf, scoring, timeparse
from mycompanion.memory import topic as topic_selector
from mycompanion.memory.models import Memory, RecallEvent
from mycompanion.memory.okf import Entry, OkfError, Relation, Source, TopicArchive, TopicDoc
from mycompanion.memory.store import MemoryStore
from mycompanion.memory.topic_store import TopicStore
from mycompanion.safety import screening

logger = logging.getLogger(__name__)

# トピックにしない記憶の種類(話題ではないため)
SKIP_KINDS = ("monthly_summary", "interaction_preference")
CANDIDATE_LIMIT = 15  # 1回の整理で LLM に渡す、既存のトピックの上限
CANDIDATE_ENTRIES = 5  # 候補1件に添える項目(最初の1件 + 直近)
DEPRECATED_KEEP_DAYS = 90  # 統合元などの現行でない文書を、消すまでの日数
REVISION_KEEP = 10  # 改訂履歴: 文書ごとに残す版の数
REVISION_KEEP_DAYS = 90
RELATED_MAX = 2  # 経過に添える、関係するトピックの数


@dataclass
class MovedMemory:
    """その日に中期へ移した(または、更新した)記憶と、それが話された時刻。"""

    memory: Memory
    told_at: datetime


@dataclass
class Timeline:
    """プロンプトに入れる、1トピックの経過(命令ではないデータとして渡す)。"""

    doc_id: uuid.UUID
    short_id: str
    title: str
    description: str
    lines: list[str]
    source_memory_ids: set[uuid.UUID] = field(default_factory=set)
    related: list[str] = field(default_factory=list)  # 関係するトピック(名前と説明だけ。最大2件)
    matched_by: str = ""  # name(名前が発話にある)/ similarity / memory(検索された記憶が根拠)

    def block(self) -> tuple[str, str, list[str]]:
        return self.title, self.description, [*self.lines, *self.related]

    def summary(self) -> dict[str, Any]:
        return {"id": self.short_id, "title": self.title, "entries": len(self.lines), "matched_by": self.matched_by}


@dataclass
class TopicOffer:
    """キャラクタが自分から出す話題の候補(トピック)。

    `kind` は topic(話題)/ followup(予定の「その後」)/ associate(関係をたどる連想)。
    """

    doc_id: uuid.UUID
    short_id: str
    title: str
    kind: str
    score: float
    text: str  # 下書きに渡す文面(データ)
    entry_key: str | None = None  # followup: 聞く予定の項目
    path: str = ""  # associate: つながりの説明(焦点 → 行き先)
    strength: float = 0.0  # associate: 連想の強さ


def _local(moment: datetime, timezone: str) -> date:
    try:
        return moment.astimezone(ZoneInfo(timezone)).date()
    except Exception:  # noqa: BLE001 - 不明なタイムゾーンは、UTC として扱う
        return moment.astimezone(UTC).date()


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def entry_line(entry: Entry) -> str:
    """LLM と利用者に見せる、項目の1行(ID、時期、種類、内容)。"""
    return f"{entry.key} {entry.occurred.label()} {entry.kind} {entry.text}"


class TopicManager:
    def __init__(self, store: TopicStore, memories: MemoryStore, llm: LLMClient, settings: EngineSettings) -> None:
        self._store = store
        self._memories = memories
        self._llm = llm
        self._settings = settings

    @property
    def store(self) -> TopicStore:
        return self._store

    async def _current(self, user_id: uuid.UUID, character_id: str) -> list[TopicDoc]:
        """現行のトピック(統合済み・現行でない文書を除く)。"""
        return [d for d in await self._store.list(user_id, character_id) if d.status != "deprecated"]

    # ------------------------------------------------------------------
    # 日次整理の手順4.5: 割り当て、作成、確立、項目の追記
    # ------------------------------------------------------------------

    async def assign_day(
        self,
        user_id: uuid.UUID,
        character_id: str,
        moved: list[MovedMemory],
        day: date,
        now: datetime,
        timezone: str = "Asia/Tokyo",
        preferences: list[tuple[list[float], float]] | None = None,
    ) -> dict[str, int]:
        counts = {"created": 0, "updated": 0, "entries": 0, "established": 0, "rejected": 0}
        inputs = [
            m for m in moved
            if m.memory.kind not in SKIP_KINDS and not m.memory.quarantined and not m.memory.core_key
            and m.memory.deleted_at is None and m.memory.summary
        ]  # fmt: skip
        if not inputs:
            return counts
        docs = await self._current(user_id, character_id)
        candidates = self._candidates(docs, inputs)
        refs: dict[str, TopicDoc] = {f"c{index}": doc for index, doc in enumerate(candidates)}
        ops = await self._llm.assign_topics(
            [
                TopicMemoryInput(
                    text=m.memory.summary, told_on=_local(m.told_at, timezone).isoformat(),
                    kind=m.memory.kind, topics=list(m.memory.topics),
                )
                for m in inputs
            ],
            [
                TopicCandidate(
                    ref, doc.kind, doc.title, list(doc.aliases), [entry_line(e) for e in self._digest(doc)]
                )
                for ref, doc in refs.items()
            ],
        )  # fmt: skip

        # --- 新しいトピック(候補として作る。同じ名前の現行のトピックがあれば、そちらに寄せる) ---
        created: set[uuid.UUID] = set()
        by_title = {doc.title: doc for doc in docs}
        for new in ops.new_topics:
            title = okf.clean_text(new.title, okf.TITLE_MAX)
            if not title or new.kind not in okf.TYPES:
                continue
            if title in by_title:
                refs[new.ref] = by_title[title]
                continue
            aliases = [a for a in (okf.clean_text(x, okf.TITLE_MAX) for x in new.aliases) if a and a != title]
            doc = TopicDoc(
                doc_id=uuid.uuid4(), path=okf.new_path(new.kind), okf_type=okf.TYPES[new.kind], title=title,
                aliases=aliases[: okf.ALIASES_MAX], created_day=day, created_at=now,
            )  # fmt: skip
            refs[new.ref] = by_title[title] = doc
            created.add(doc.doc_id)

        # --- 項目の内容が、根拠の記憶から離れていないかを確かめる(元の発話にない事実を作らない) ---
        entry_ops = [op for op in ops.entries if op.topic in refs and 0 <= op.memory < len(inputs) and op.text.strip()]
        vectors = await self._llm.embed([op.text for op in entry_ops]) if entry_ops else []

        touched: dict[uuid.UUID, TopicDoc] = {}
        corrected: set[uuid.UUID] = set()
        for op, vector in zip(entry_ops, vectors, strict=True):
            source = inputs[op.memory]
            if scoring.cosine(vector, source.memory.embedding) < self._settings.topic_entry_min_grounding:
                counts["rejected"] += 1
                continue
            doc = refs[op.topic]
            if self._add_entry(doc, op, source, timezone, now):
                touched[doc.doc_id] = doc
                counts["entries"] += 1
                if op.kind == "correction":
                    corrected.add(doc.doc_id)

        # --- トピック同士の関係(ユーザーが明言したものだけ。検査して、関係を持つ側の文書に書く) ---
        everything = {d.path: d for d in [*docs, *refs.values()]}
        for relation in ops.relations:
            if relation.source not in refs or relation.target not in refs or not 0 <= relation.memory < len(inputs):
                continue
            owner = self._relate(refs[relation.source], refs[relation.target], relation, inputs[relation.memory], everything, timezone, now)
            if owner is None:
                counts["rejected"] += 1
            else:
                touched[owner.doc_id] = owner
                counts["relations"] = counts.get("relations", 0) + 1

        # --- 説明、タグ、確立の判定、鮮度。文書を検査して、保存する ---
        ref_of = {doc.doc_id: ref for ref, doc in refs.items()}
        # 項目のない文書(関係だけを持つ、作りかけの候補)は、保存しない
        touched = {doc_id: doc for doc_id, doc in touched.items() if doc.entries}
        changed = list(touched.values())
        embeddings = (
            await self._llm.embed(
                [" ".join([d.title, *d.aliases, ops.descriptions.get(ref_of[d.doc_id], d.description)]) for d in changed]
            )
            if changed
            else []
        )
        for doc, embedding in zip(changed, embeddings, strict=True):
            description = okf.clean_text(ops.descriptions.get(ref_of[doc.doc_id], ""), okf.DESCRIPTION_MAX)
            if description and not screening.is_suspicious_memory(description):
                # 説明は、前の説明からではなく、項目から作り直したものに置き換える
                doc.description = description
            doc.embedding = embedding
            # 嗜好度: トピックの名前・別名・説明と、キャラクタの好みを照合する(事実は変えない)
            doc.preference = round(scoring.preference_score([embedding], preferences), 4) if preferences else 0.0
            doc.generated_by = f"mc_consolidation/{ops.model_id or 'unknown'}"
            doc.generated_at = doc.updated_at = now
            was_established = doc.established
            self._establish(doc, [m.memory for m in inputs], now)
            self._refresh_stale_after(doc)
            if doc.lifecycle == "dormant":
                doc.lifecycle = "active"  # ユーザーが触れたので、休止から戻す
            try:
                okf.validate(doc)
            except OkfError as exc:
                logger.warning("トピックの文書が、検査に通りませんでした: %s", type(exc).__name__)  # 本文を含みうる
                counts["rejected"] += 1
                continue
            is_new = doc.doc_id in created
            kind = "creation" if is_new else "correction" if doc.doc_id in corrected else "update"
            await self._store.save(user_id, character_id, doc, kind, day)
            counts["created" if is_new else "updated"] += 1
            if doc.established and not was_established:
                counts["established"] += 1
        return counts

    def _candidates(self, docs: list[TopicDoc], inputs: list[MovedMemory]) -> list[TopicDoc]:
        """照合の候補。記憶ごとに、名前が本文にあるものと、埋め込みの近いものを取り、合わせる。"""
        chosen: dict[uuid.UUID, tuple[float, TopicDoc]] = {}
        for item in inputs:
            scored = []
            for doc in docs:
                named = any(len(n) >= 2 and n in item.memory.summary for n in (doc.title, *doc.aliases))
                score = 2.0 if named else scoring.cosine(item.memory.embedding, doc.embedding)
                scored.append((score, doc))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            for score, doc in scored[: self._settings.topic_candidate_k]:
                if doc.doc_id not in chosen or chosen[doc.doc_id][0] < score:
                    chosen[doc.doc_id] = (score, doc)
        ranked = sorted(chosen.values(), key=lambda pair: pair[0], reverse=True)
        return [doc for _, doc in ranked[:CANDIDATE_LIMIT]]

    @staticmethod
    def _digest(doc: TopicDoc, limit: int = CANDIDATE_ENTRIES) -> list[Entry]:
        """最初の1件 + 直近の項目(訂正済み・隔離を除く)。"""
        items = [e for e in doc.sorted_entries() if e.active]
        return items if len(items) <= limit else [items[0], *items[-(limit - 1) :]]

    def _add_entry(self, doc: TopicDoc, op: TopicEntryOp, source: MovedMemory, timezone: str, now: datetime) -> bool:
        memory = source.memory
        text = okf.clean_text(op.text, okf.ENTRY_MAX)
        key = okf.source_key(memory.memory_id)
        if not text or op.kind not in okf.ENTRY_KINDS:
            return False
        if any(key in e.source_keys and e.text == text for e in doc.entries):
            return False  # 同じ記憶からの、同じ項目(再実行しても、重複しない)
        told_on = _local(source.told_at, timezone)
        # 時期: よく出る表現はコードで決める。食い違ったら、コードの値を使う。それ以外は、LLM の解釈を検査して使う
        occurred = timeparse.normalize(op.time_expression, told_on) or timeparse.check_range(
            _parse_date(op.occurred_from), _parse_date(op.occurred_to), op.precision, told_on, future=op.kind == "plan"
        )
        entry = Entry(
            key=f"t{doc.next_entry_no}", kind=op.kind, text=text, occurred=occurred, told_on=told_on,
            source_keys=[key], told_at=source.told_at, expression=op.time_expression or None,
            valence=(memory.emotion_vad or {}).get("valence"), sensitivity=memory.sensitivity,
        )  # fmt: skip
        active = {e.key: e for e in doc.entries if e.active}
        if op.kind == "correction" and op.supersedes in active:
            active[op.supersedes].superseded_by = entry.key  # 訂正: 元の項目は「訂正済み」に移す
        if op.kind == "outcome" and op.resolves in active and active[op.resolves].kind == "plan":
            entry.resolves = op.resolves
        doc.next_entry_no += 1
        doc.entries.append(entry)
        if doc.source(key) is None:
            doc.sources.append(Source(key, memory.memory_id, told_on, now))
        # タグ: 割り当てた記憶の話題タグ(照合に使う)
        doc.tags = list(dict.fromkeys([*doc.tags, *memory.topics]))[: okf.TAGS_MAX]
        return True

    def _relate(
        self,
        source: TopicDoc,
        target: TopicDoc,
        op: TopicRelationOp,
        grounding: MovedMemory,
        docs: dict[str, TopicDoc],
        timezone: str,
        now: datetime,
    ) -> TopicDoc | None:
        """関係を検査して、文書に加える。加えた文書を返す。検査に通らなければ None(何もしない)。

        検査: 両端の型の組み合わせ、根拠の記憶に両方のトピックが出ていること(関係の創作の防止)、
        循環と深さ(親子は2段まで、親は1つ)、件数の上限、重複。
        """
        if source.doc_id == target.doc_id or op.kind not in okf.RELATION_KINDS:
            return None
        if op.kind == "parallel_with" and str(target.doc_id) < str(source.doc_id):
            source, target = target, source  # 対称の関係は、片側にだけ保存する
        role = op.role if op.kind == "involves" else None
        if (source.kind, target.kind) not in okf.RELATION_ENDS[op.kind] or (op.kind == "involves") != (
            role in okf.RELATION_ROLES
        ):
            return None
        memory, key = grounding.memory, okf.source_key(grounding.memory.memory_id)

        def mentioned(doc: TopicDoc) -> bool:
            named = any(len(n) >= 2 and n in memory.summary for n in (doc.title, *doc.aliases))
            return named or any(key in e.source_keys for e in doc.entries)

        if not source.entries or not mentioned(source) or not mentioned(target):
            return None
        if any(r.to == target.path and r.kind == op.kind for r in source.relations):
            return None
        if any(r.to == source.path and r.kind == op.kind for r in target.relations):
            return None  # 逆向きに同じ関係がある(循環、または対称の重複)

        def parents(doc: TopicDoc, kind: str) -> list[str]:
            return [r.to for r in doc.relations if r.kind == kind]

        def reaches(start: TopicDoc, goal: str, kind: str) -> bool:
            seen, stack = set(), [start.path]
            while stack:
                path = stack.pop()
                if path == goal:
                    return True
                if path in seen or path not in docs:
                    continue
                seen.add(path)
                stack += parents(docs[path], kind)
            return False

        if op.kind in ("part_of", "member_of", "follows") and reaches(target, source.path, op.kind):
            return None  # 循環
        if op.kind == "part_of":
            has_children = any(source.path in parents(d, "part_of") for d in docs.values())
            if parents(source, "part_of") or parents(target, "part_of") or has_children:
                return None  # 親は1つ。親子は2段まで
        if op.kind == "follows" and parents(source, "follows"):
            return None  # 先行する出来事は、1つ
        involves = sum(r.kind == "involves" for r in source.relations)
        if len(source.relations) >= okf.RELATIONS_MAX or (op.kind == "involves" and involves >= okf.INVOLVES_MAX):
            return None
        number = max((int(r.key[1:]) for r in source.relations), default=0) + 1
        source.relations.append(Relation(f"r{number}", target.path, op.kind, target.title, role, "stated", [key]))
        if source.source(key) is None:
            source.sources.append(Source(key, memory.memory_id, _local(grounding.told_at, timezone), now))
        return source

    def _establish(self, doc: TopicDoc, memories: list[Memory], now: datetime) -> None:
        """確立の判定。昇格の条件に合わせる(長く残るのは、長期に相当する話題の項目だけ)。"""
        if doc.established:
            return
        settings = self._settings
        keys = {s.key for s in doc.sources}
        related = [m for m in memories if okf.source_key(m.memory_id) in keys]
        days = {e.told_on for e in doc.entries}
        if (
            len(days) >= settings.topic_establish_min_days
            or any(m.pinned or m.importance >= settings.memory_promotion_importance for m in related)
        ):
            doc.status, doc.lifecycle, doc.established_at = "stable", "active", now

    @staticmethod
    def _refresh_stale_after(doc: TopicDoc) -> None:
        """予定の時期が過ぎ、結果がまだないとき、「その後」を聞いてよくなる時刻(予定の終わり + 1日)。"""
        resolved = {e.resolves for e in doc.entries if e.resolves}
        pending = [
            e.occurred.end for e in doc.entries
            if e.active and e.kind == "plan" and e.key not in resolved and e.occurred.end is not None
        ]  # fmt: skip
        doc.stale_after = (
            datetime.combine(min(pending) + timedelta(days=1), time.min, tzinfo=UTC) if pending else None
        )

    # ------------------------------------------------------------------
    # 検索の枠(プロンプトに入れる、1トピックの経過)
    # ------------------------------------------------------------------

    async def timeline_for(
        self,
        user_id: uuid.UUID,
        character_id: str,
        text: str,
        query: list[float],
        retrieved_ids: set[uuid.UUID],
    ) -> Timeline | None:
        """会話の話題に合う、確立したトピックを1件選ぶ。合うものがなければ None。

        合う条件: ①トピックの名前・別名が発話にある、②発話とトピックの埋め込みが近い、
        ③ワーキングセットの記憶(この会話で、実際に使った記憶)が、そのトピックの根拠になっている。
        """
        settings = self._settings
        best: tuple[float, TopicDoc, str] | None = None
        candidates = await self._current(user_id, character_id)
        for doc in candidates:
            if not doc.established or doc.quarantined or doc.lifecycle == "merged":
                continue
            names = [n for n in (doc.title, *doc.aliases) if len(n) >= 2 and n in text]
            similarity = scoring.cosine(query, doc.embedding)
            overlap = len({s.memory_id for s in doc.sources if s.memory_id} & retrieved_ids)
            if names:
                score, how = 2.0 + max(len(n) for n in names) / 100, "name"
            elif similarity >= settings.topic_query_min_similarity:
                score, how = 1.0 + similarity, "similarity"
            elif overlap:
                score, how = 0.5 + min(overlap, 4) / 10, "memory"
            else:
                continue
            if best is None or score > best[0]:
                best = (score, doc, how)
        if best is None:
            return None
        _, doc, how = best
        # センシティブな項目は、ユーザーがそのターンで、その話題に触れたときだけ入れる
        touched = how in ("name", "similarity")
        entries = [e for e in doc.sorted_entries() if e.active and (touched or not e.sensitivity)]
        if not entries:
            return None
        limit = settings.topic_timeline_max_entries
        chosen = entries if len(entries) <= limit else [entries[0], *entries[-(limit - 1) :]]
        resolved = {e.resolves for e in doc.entries if e.resolves}
        plans = [e for e in entries if e.kind == "plan" and e.key not in resolved]
        if plans and plans[-1] not in chosen:
            chosen = [chosen[0], plans[-1], *chosen[2:]] if len(chosen) > 1 else [plans[-1]]
            chosen.sort(key=entries.index)
        lines, used = [], 0
        for entry in chosen:
            mark = "(予定)" if entry.kind == "plan" and entry.key not in resolved else ""
            line = f"{entry.occurred.label() if entry.occurred.start else entry.told_on.isoformat() + ' ごろに話した'}: {mark}{entry.text}"
            if used + len(line) > settings.topic_timeline_char_budget and lines:
                break
            lines.append(line)
            used += len(line)
        keys = {key for entry in chosen for key in entry.source_keys}
        sources = {s.memory_id for s in doc.sources if s.key in keys and s.memory_id}
        timeline = Timeline(doc.doc_id, doc.short_id, doc.title, doc.description, lines, sources, matched_by=how)
        # 関係する確立したトピックを、名前と説明だけ、最大2件(関与の相手 > 親 > 前の出来事 > 逆向き)
        order = {"involves": 0, "part_of": 1, "member_of": 1, "follows": 2, "parallel_with": 3}
        neighbors = [
            (other, relation, forward)
            for other, relation, forward in self.related(doc, candidates)
            if other.established and not other.quarantined and not any(e.sensitivity for e in other.entries)
        ]
        neighbors.sort(key=lambda item: (not item[2], order[item[1].kind]))
        for other, relation, forward in neighbors[:RELATED_MAX]:
            label = relation.label if forward else "関係する話題"
            timeline.related.append(f"({label}){other.title}: {other.description}")
        return timeline

    async def _delete_doc(self, user_id: uuid.UUID, character_id: str, doc: TopicDoc, now: datetime) -> None:
        """文書を消す。ほかの文書に残る、この文書への関係も消す(相手の文書の mc_relations と「# 関係」から)。"""
        await self._store.delete(user_id, character_id, doc.doc_id)
        for other in await self._store.list(user_id, character_id):
            kept = [r for r in other.relations if r.to != doc.path]
            if len(kept) != len(other.relations):
                other.relations = kept
                other.generated_by, other.generated_at, other.updated_at = "process:unlink", now, now
                await self._store.save(user_id, character_id, other, "unlink")

    def related(self, doc: TopicDoc, docs: list[TopicDoc]) -> list[tuple[TopicDoc, Relation, bool]]:
        """関係でつながるトピック。(相手, 関係, 正の向きか)。逆向きは、相手の文書から引く。"""
        by_path = {d.path: d for d in docs}
        found = [(by_path[r.to], r, True) for r in doc.relations if r.to in by_path]
        found += [(other, r, False) for other in docs for r in other.relations if r.to == doc.path]
        return found

    @staticmethod
    def muted_by_relation(docs: list[TopicDoc]) -> set[uuid.UUID]:
        """「持ち出さないで」の伝播: 親が mute なら子も、エンティティが mute なら、それが関わる出来事も。"""
        muted_paths = {d.path for d in docs if d.muted}
        return {
            d.doc_id for d in docs
            if d.muted or any(r.to in muted_paths and r.kind in ("part_of", "involves") for r in d.relations)
        }  # fmt: skip

    # ------------------------------------------------------------------
    # 削除の伝播と、期限切れ
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_sources(doc: TopicDoc, keys: set[str]) -> None:
        removed = {e.key for e in doc.entries if set(e.source_keys) & keys}
        doc.entries = [e for e in doc.entries if e.key not in removed]
        for entry in doc.entries:
            if entry.superseded_by in removed:
                entry.superseded_by = None
            if entry.resolves in removed:
                entry.resolves = None
        doc.sources = [s for s in doc.sources if s.key not in keys]
        # 関係の根拠からも消す。根拠がなくなった関係は、消す
        for relation in doc.relations:
            relation.source_keys = [k for k in relation.source_keys if k not in keys]
        doc.relations = [r for r in doc.relations if r.source_keys]

    async def on_memories_deleted(
        self, user_id: uuid.UUID, character_id: str, memory_ids: set[uuid.UUID], now: datetime
    ) -> int:
        """ユーザーが記憶を削除した。その記憶が出典の項目を、**確立していても**消す。概要も取り下げる。"""
        affected = 0
        for doc in await self._store.list(user_id, character_id):
            keys = {s.key for s in doc.sources if s.memory_id in memory_ids}
            if not keys:
                continue
            affected += 1
            self._remove_sources(doc, keys)
            if not doc.entries:
                await self._delete_doc(user_id, character_id, doc, now)
                continue
            # 説明は、消した記憶に触れている可能性があるので、次の整理で作り直すまで空にする
            doc.description = ""
            doc.generated_by, doc.generated_at, doc.updated_at = "process:deletion", now, now
            self._refresh_stale_after(doc)
            await self._store.save(user_id, character_id, doc, "deletion_propagation")
            await self._store.drop_revisions(user_id, character_id, doc.doc_id)
        for archive in await self._store.archives(user_id, character_id):
            if set(archive.memory_ids) & memory_ids:
                # 概要に、削除した記憶の内容が含まれうるので、取り下げる(名前と期間だけ残す)
                archive.summary, archive.summary_state, archive.pending_lines = "", "withdrawn", []
                archive.memory_ids = [m for m in archive.memory_ids if m not in memory_ids]
                await self._store.update_archive(user_id, character_id, archive)
                affected += 1
        return affected

    async def on_memories_expired(
        self, user_id: uuid.UUID, character_id: str, memory_ids: set[uuid.UUID], now: datetime
    ) -> int:
        """中期の記憶が、期限切れで消える。候補のトピックは項目も消し、確立したトピックは項目を残す。"""
        affected = 0
        for doc in await self._store.list(user_id, character_id):
            sources = [s for s in doc.sources if s.memory_id in memory_ids]
            if not sources:
                continue
            affected += 1
            if doc.established:
                for source in sources:
                    source.expired, source.memory_id = True, None
            else:
                self._remove_sources(doc, {s.key for s in sources})
                if not doc.entries:
                    await self._delete_doc(user_id, character_id, doc, now)
                    continue
            doc.generated_by, doc.generated_at, doc.updated_at = "process:expiry", now, now
            await self._store.save(user_id, character_id, doc, "expiry")
        return affected

    # ------------------------------------------------------------------
    # 利用者の操作(一覧、表示、持ち出さない、削除 = アーカイブ、完全な削除)
    # ------------------------------------------------------------------

    @staticmethod
    def view(doc: TopicDoc, detail: bool = False) -> dict[str, Any]:
        entries = [e for e in doc.sorted_entries() if e.active]
        data: dict[str, Any] = {
            "id": doc.short_id,
            "kind": doc.kind,
            "title": doc.title,
            "description": doc.description,
            "status": doc.status,
            "muted": doc.muted,
            "entries": len(entries),
            "first_occurred": doc.frontmatter().get("first_occurred"),
            "last_occurred": doc.frontmatter().get("last_occurred"),
        }
        if detail:
            data["aliases"] = list(doc.aliases)
            data["relations"] = [
                {"id": r.key, "label": r.label, "kind": r.kind, "to": r.to.rsplit("/", 1)[-1].removesuffix(".md"),
                 "title": r.to_title}
                for r in doc.relations
            ]  # fmt: skip
            data["timeline"] = [
                {"id": e.key, "when": e.occurred.label(), "told_on": e.told_on.isoformat(), "kind": e.kind, "text": e.text}
                for e in entries
            ]
            data["document"] = doc.markdown()
        return data

    async def find(self, user_id: uuid.UUID, character_id: str, short_id: str) -> TopicDoc | None:
        short_id = short_id.strip().lower()
        return next((d for d in await self._current(user_id, character_id) if d.short_id == short_id), None)

    async def list_topics(
        self, user_id: uuid.UUID, character_id: str, include_candidates: bool = False
    ) -> list[dict[str, Any]]:
        docs = [d for d in await self._current(user_id, character_id) if include_candidates or d.established]
        return [self.view(d) for d in sorted(docs, key=lambda d: (d.okf_type, d.title))]

    async def set_muted(self, user_id: uuid.UUID, character_id: str, short_id: str, muted: bool, now: datetime) -> bool:
        doc = await self.find(user_id, character_id, short_id)
        if doc is None:
            return False
        doc.muted, doc.updated_at = muted, now
        await self._store.save(user_id, character_id, doc, "user_edit")
        return True

    async def unlink(self, user_id: uuid.UUID, character_id: str, short_id: str, key: str, now: datetime) -> bool:
        """ユーザーによる、関係の取り消し。"""
        doc = await self.find(user_id, character_id, short_id)
        if doc is None or all(r.key != key for r in doc.relations):
            return False
        doc.relations = [r for r in doc.relations if r.key != key]
        doc.verified = [*doc.verified, {"by": "human:user", "at": now.isoformat()}]
        doc.updated_at = now
        await self._store.save(user_id, character_id, doc, "user_edit")
        return True

    async def archive(self, user_id: uuid.UUID, character_id: str, short_id: str, now: datetime) -> TopicArchive | None:
        """トピックの削除 = アーカイブ。概要を1回だけ作って残し、文書・項目・索引・改訂履歴は消す。"""
        doc = await self.find(user_id, character_id, short_id)
        if doc is None:
            return None
        memory_ids = [s.memory_id for s in doc.sources if s.memory_id]
        memories = {m: await self._memories.get(user_id, character_id, m) for m in memory_ids}
        # センシティブ・隔離・「持ち出さないで」の記憶が出典の項目は、概要に入れない
        hidden = {
            okf.source_key(m) for m, memory in memories.items()
            if memory is not None and (memory.sensitivity or memory.quarantined or memory.muted)
        }  # fmt: skip
        lines = [
            entry_line(e) for e in doc.sorted_entries()
            if e.active and not e.sensitivity and not set(e.source_keys) & hidden
        ]  # fmt: skip
        front = doc.frontmatter()
        archive = TopicArchive(
            archive_id=uuid.uuid4(), original_doc_id=doc.doc_id, path=okf.new_archive_path(), title=doc.title,
            summary="", summary_state="ok", archived_at=now, memory_ids=list(memory_ids),
            first_occurred=_parse_date(str(front.get("first_occurred", ""))),
            last_occurred=_parse_date(str(front.get("last_occurred", ""))),
        )  # fmt: skip
        if lines:
            try:
                archive.summary = self._screened(await self._llm.summarize_topic(doc.title, lines))
            except Exception as exc:  # noqa: BLE001 - 概要なしでアーカイブし、次の日次整理で1回だけ再試行する
                logger.warning("アーカイブの概要を作れませんでした: %s", type(exc).__name__)
                archive.summary_state, archive.pending_lines = "pending", lines
        await self._store.add_archive(user_id, character_id, archive)
        await self._delete_doc(user_id, character_id, doc, now)
        # このトピックにだけ属していた記憶は、想起の対象から外す(期限・昇格の判定は、今までどおり)
        elsewhere = {
            s.memory_id for other in await self._store.list(user_id, character_id) for s in other.sources if s.memory_id
        }
        for memory_id in memory_ids:
            if memory_id not in elsewhere:
                await self._memories.update(user_id, character_id, memory_id, recall_excluded="topic_archived")
        return archive

    @staticmethod
    def _screened(summary: str) -> str:
        summary = okf.clean_text(summary, okf.ARCHIVE_SUMMARY_MAX)
        return "" if screening.is_suspicious_memory(summary) else summary

    async def archives(self, user_id: uuid.UUID, character_id: str) -> list[dict[str, Any]]:
        """アーカイブの一覧(利用者への表示・書き出し用。生成の経路からは呼ばない)。"""
        return [a.view() for a in await self._store.archives(user_id, character_id)]

    async def purge(self, user_id: uuid.UUID, character_id: str, short_id: str) -> bool:
        """アーカイブの完全な削除(概要も消す)。"""
        short_id = short_id.strip().lower()
        for archive in await self._store.archives(user_id, character_id):
            if archive.short_id == short_id:
                return await self._store.purge_archive(user_id, character_id, archive.archive_id)
        return False

    async def bundle(self, user_id: uuid.UUID, character_id: str) -> dict[str, str]:
        """書き出し用の OKF バンドル(パス → 文書)。キャラクタごとに別。アーカイブは `archive/` に入れる。"""
        docs = await self._current(user_id, character_id)
        files = {"index.md": okf.index_markdown(docs)}
        files |= {doc.path: doc.markdown() for doc in docs}
        files |= {a.path: a.markdown() for a in await self._store.archives(user_id, character_id)}
        return files

    # ------------------------------------------------------------------
    # 定期の見直し(日次整理のあとに行う)
    # ------------------------------------------------------------------

    async def review(self, user_id: uuid.UUID, character_id: str, now: datetime) -> dict[str, int]:
        """名前の近いトピックの統合、休止、根拠のなくなった候補の削除、改訂履歴の整理、概要の再試行。

        日次整理のたびに行う(記憶の整理と、同時・同じ頻度)。
        """
        settings = self._settings
        counts = {"merged": 0, "dormant": 0, "removed": 0, "revisions_pruned": 0, "summaries_retried": 0}
        try:
            counts["merged"] = await self.merge_similar(user_id, character_id, now)
        except Exception as exc:  # noqa: BLE001 - 統合に失敗しても、ほかの見直しは続ける
            logger.warning("トピックの統合に失敗しました: %s", type(exc).__name__)
        for doc in await self._store.list(user_id, character_id):
            last = max((e.told_on for e in doc.entries), default=None)
            updated = doc.updated_at or doc.created_at or now
            if doc.status == "deprecated" and (now - updated).days >= DEPRECATED_KEEP_DAYS or not doc.entries:
                await self._delete_doc(user_id, character_id, doc, now)
                counts["removed"] += 1
            elif doc.lifecycle == "active" and last and (now.date() - last).days >= settings.topic_dormant_days:
                doc.lifecycle, doc.updated_at = "dormant", now
                await self._store.save(user_id, character_id, doc, "dormant")
                counts["dormant"] += 1
        counts["revisions_pruned"] = await self._store.prune_revisions(
            user_id, character_id, REVISION_KEEP, now - timedelta(days=REVISION_KEEP_DAYS)
        )
        for archive in await self._store.archives(user_id, character_id):
            if archive.summary_state != "pending":
                continue
            lines, archive.pending_lines = archive.pending_lines, []  # 再試行は1回だけ。項目は、もう持たない
            try:
                archive.summary = self._screened(await self._llm.summarize_topic(archive.title, lines)) if lines else ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("アーカイブの概要の再試行に失敗しました: %s", type(exc).__name__)
            archive.summary_state = "ok"
            await self._store.update_archive(user_id, character_id, archive)
            counts["summaries_retried"] += 1
        return counts

    # ------------------------------------------------------------------
    # 統合(名前の近いトピックを、1つにする。日次整理のたび)
    # ------------------------------------------------------------------

    def _candidate(self, ref: str, doc: TopicDoc) -> TopicCandidate:
        return TopicCandidate(ref, doc.kind, doc.title, list(doc.aliases), [entry_line(e) for e in self._digest(doc)])

    async def merge_similar(self, user_id: uuid.UUID, character_id: str, now: datetime) -> int:
        """名前の近いトピックの組を、LLM が「同じ」と判定したときだけ、古いほうに統合する。統合した数を返す。

        別の人物・別のペットを1つにまとめる誤りは、分かれたままの誤りより重い。迷ったら、統合しない。
        """
        settings = self._settings
        docs = await self._current(user_id, character_id)
        pairs: list[tuple[float, TopicDoc, TopicDoc]] = []
        for index, first in enumerate(docs):
            for second in docs[index + 1 :]:
                if first.kind != second.kind:
                    continue
                # 候補: 名前・別名が同じ、片方の名前が、もう片方に含まれる(「タマ」と「タマちゃん」)、埋め込みが近い
                shared = bool({first.title, *first.aliases} & {second.title, *second.aliases}) or (
                    min(len(first.title), len(second.title)) >= 2
                    and (first.title in second.title or second.title in first.title)
                )
                similarity = scoring.cosine(first.embedding, second.embedding)
                if shared or similarity >= settings.topic_merge_similarity:
                    pairs.append((2.0 if shared else similarity, first, second))
        pairs.sort(key=lambda item: item[0], reverse=True)
        gone: set[uuid.UUID] = set()
        merged = 0
        for _, first, second in pairs[: settings.topic_merge_max_pairs]:
            if first.doc_id in gone or second.doc_id in gone:
                continue
            if not await self._llm.same_topic(self._candidate("a", first), self._candidate("b", second)):
                continue
            # 統合先は、古いほう(パスが変わらないように)
            target, source = sorted((first, second), key=lambda d: (d.created_at or now, d.path))
            snapshot = await self._store.get(user_id, character_id, target.doc_id)
            self._absorb(target, source)
            target.generated_by, target.generated_at, target.updated_at = "process:merge", now, now
            self._refresh_stale_after(target)
            try:
                okf.validate(target)
            except OkfError as exc:
                logger.warning("統合した文書が、検査に通りませんでした: %s", type(exc).__name__)  # 本文を含みうる
                if snapshot is not None:
                    docs[docs.index(target)] = snapshot
                continue
            await self._store.save(user_id, character_id, target, "merge")
            await self._store.delete(user_id, character_id, source.doc_id)
            gone.add(source.doc_id)
            merged += 1
            # ほかの文書の、統合元への関係を、統合先に付け替える(重複と、自分自身への関係は消す)
            for other in await self._store.list(user_id, character_id):
                if all(r.to != source.path for r in other.relations):
                    continue
                kept: list[Relation] = []
                for relation in other.relations:
                    if relation.to == source.path:
                        relation.to, relation.to_title = target.path, target.title
                    duplicate = any(k.to == relation.to and k.kind == relation.kind for k in kept)
                    if relation.to != other.path and not duplicate:
                        kept.append(relation)
                other.relations, other.updated_at = kept, now
                await self._store.save(user_id, character_id, other, "merge")
        return merged

    @staticmethod
    def _absorb(target: TopicDoc, source: TopicDoc) -> None:
        """統合元の項目・出典・別名・関係を、統合先に移す。項目の ID は、統合先で振り直す。"""
        keys: dict[str, str] = {}
        moved: list[Entry] = []
        for entry in sorted(source.entries, key=lambda e: int(e.key[1:])):
            duplicate = next(
                (e for e in target.entries if e.text == entry.text and set(e.source_keys) == set(entry.source_keys)), None
            )
            if duplicate is not None:
                keys[entry.key] = duplicate.key
                continue
            keys[entry.key] = f"t{target.next_entry_no}"
            target.next_entry_no += 1
            entry.key = keys[entry.key]
            target.entries.append(entry)
            moved.append(entry)
        for entry in moved:  # 統合元の項目どうしの参照(訂正、予定の結果)を、新しい ID に付け替える
            entry.superseded_by = keys.get(entry.superseded_by) if entry.superseded_by else None
            entry.resolves = keys.get(entry.resolves) if entry.resolves else None
        known = {s.key for s in target.sources}
        target.sources += [s for s in source.sources if s.key not in known]
        names = [n for n in dict.fromkeys([*target.aliases, source.title, *source.aliases]) if n != target.title]
        target.aliases = names[: okf.ALIASES_MAX]
        target.tags = list(dict.fromkeys([*target.tags, *source.tags]))[: okf.TAGS_MAX]
        target.description = target.description or source.description
        for relation in source.relations:
            exists = any(r.to == relation.to and r.kind == relation.kind for r in target.relations)
            if relation.to != target.path and not exists and len(target.relations) < okf.RELATIONS_MAX:
                number = max((int(r.key[1:]) for r in target.relations), default=0) + 1
                relation.key = f"r{number}"
                target.relations.append(relation)
        target.relations = [r for r in target.relations if r.to != source.path]
        target.followup_keys = list(dict.fromkeys([*target.followup_keys, *(keys.get(k, k) for k in source.followup_keys)]))
        target.activation = max(target.activation, source.activation)
        target.muted = target.muted or source.muted  # 「持ち出さないで」は、引き継ぐ
        if source.established and not target.established:
            target.status, target.lifecycle, target.established_at = "stable", "active", source.established_at

    # ------------------------------------------------------------------
    # 第2段階: 話題選択の候補、予定の「その後どうなった?」、トピックの想起と活性度
    # ------------------------------------------------------------------

    def _unresolved_plan(self, doc: TopicDoc, now: datetime) -> tuple[Entry, int] | None:
        """「その後」を聞いてよい予定(時期が過ぎ、結果を聞いていない。聞いてよい期間の中)と、過ぎてからの日数。"""
        settings = self._settings
        if doc.stale_after is None or now < doc.stale_after:
            return None
        days = (now - doc.stale_after).days
        if days > settings.topic_followup_window_days:
            return None  # 古い話を、掘り返さない
        if doc.last_followup_at and (now - doc.last_followup_at).days < settings.topic_followup_cooldown_days:
            return None
        resolved = {e.resolves for e in doc.entries if e.resolves}
        today = now.date()
        plans = [
            e for e in doc.sorted_entries()
            if e.active and e.kind == "plan" and e.key not in resolved and e.key not in doc.followup_keys
            and e.occurred.end is not None and e.occurred.end < today
        ]  # fmt: skip
        if not plans:
            return None
        plan = plans[-1]
        latest = max((e for e in doc.entries if e.active), key=lambda e: e.told_on)
        # つらい話は、キャラクタから蒸し返さない。センシティブな項目も、聞かない
        if plan.sensitivity or any((e.valence or 0.0) < topic_selector.NEGATIVE_VALENCE for e in (plan, latest)):
            return None
        return plan, days

    async def offer(
        self,
        user_id: uuid.UUID,
        character_id: str,
        query: list[float] | None,
        now: datetime,
        session_id: uuid.UUID,
        under_18: bool = False,
        exclude: set[uuid.UUID] | None = None,
    ) -> TopicOffer | None:
        """キャラクタが自分から出す話題の候補を、確立したトピックから1件選ぶ(§8.7)。なければ None。

        予定の「その後」を聞ける候補があれば、経過の適合(過ぎてからの日数が短いほど高い)を加点する。
        負の懸念(病気、別れなど)の「その後」は、聞かない。対象は、予定だけ。
        """
        settings = self._settings
        events = await self._store.recalls(user_id, character_id)
        session_events = [e for e in events if e.session_id == session_id]
        # 予定の時期のあとに、ユーザーがその話題に触れていたら、もう聞かない(結果は、次の日次整理で項目になる)
        touched: dict[uuid.UUID, date] = {}
        for event in events:
            if event.trigger == "user":
                touched[event.memory_id] = max(touched.get(event.memory_id, event.day), event.day)
        followups = sum(1 for e in session_events if e.source == "followup")
        cooldown = timedelta(days=settings.proactive_mention_cooldown_days)
        best: TopicOffer | None = None
        current = await self._current(user_id, character_id)
        muted = self.muted_by_relation(current)
        for doc in current:
            if not doc.established or doc.doc_id in muted or doc.quarantined or doc.lifecycle != "active":
                continue
            if exclude and doc.doc_id in exclude:
                continue  # いま話している話題は、振り直さない
            entries = [e for e in doc.sorted_entries() if e.active]
            if not entries or any(e.sensitivity for e in entries):
                continue
            latest = max(entries, key=lambda e: e.told_on)
            if (latest.valence or 0.0) < topic_selector.NEGATIVE_VALENCE:
                continue
            if under_18 and topic_selector.is_romantic(" ".join([doc.title, *(e.text for e in entries)])):
                continue
            if doc.last_mentioned_by_character_at and now - doc.last_mentioned_by_character_at < cooldown:
                continue
            mentioned = doc.last_mentioned_by_character_at
            freshness = 1.0 if mentioned is None else 1.0 - 2.0 ** (-max(0, (now - mentioned).days) / 14.0)
            score = (
                topic_selector.B1_PREFERENCE * doc.preference
                + topic_selector.B2_ACTIVATION * scoring.clip(doc.activation / scoring.ACTIVATION_NORMALIZER, 0.0, 1.0)
                + topic_selector.B3_RELEVANCE * max(scoring.cosine(query, doc.embedding), 0.0)
                + topic_selector.B4_FRESHNESS * freshness
                + topic_selector.B5_EMOTION_FIT * 0.5
            )
            kind, key, text = "topic", None, f"{doc.title}: {doc.description or latest.text}"
            pending = self._unresolved_plan(doc, now) if settings.topic_followup_enabled else None
            if (
                pending is not None
                and doc.stale_after is not None
                and touched.get(doc.doc_id, date.min) >= doc.stale_after.date()
            ):
                pending = None
            if pending is not None and followups < settings.topic_followup_max_per_session:
                plan, days = pending
                score += settings.topic_followup_weight * (1.0 - days / (settings.topic_followup_window_days + 1))
                kind, key = "followup", plan.key
                text = (
                    f"{doc.title}: 「{plan.text}」は、その後どうなったか、まだ聞いていない。"
                    "聞くなら、1回だけ、軽く、答えなくてもよい形で(日付や回数は言わない)"
                )
            if score >= topic_selector.MIN_SCORE and (best is None or score > best.score):
                best = TopicOffer(doc.doc_id, doc.short_id, doc.title, kind, round(score, 4), text, key)
        return best

    # ------------------------------------------------------------------
    # 自発発話の話題(§15.1 の M8): 予定の「その後」、負の懸念の「その後」、話題選択(類似による連想を含む)
    # ------------------------------------------------------------------

    def _blocked(self, doc: TopicDoc, muted: set[uuid.UUID], under_18: bool, allow_negative: bool = False) -> bool:
        """キャラクタから持ち出さないトピック(候補・休止・「持ち出さないで」・隔離・つらい話・18歳未満の恋愛的な内容)。"""
        entries = [e for e in doc.entries if e.active]
        latest = max(entries, key=lambda e: e.told_on, default=None)
        return (
            not doc.established or doc.lifecycle != "active" or doc.doc_id in muted or doc.quarantined
            or latest is None
            or (not allow_negative and (latest.valence or 0.0) < topic_selector.NEGATIVE_VALENCE)
            or (under_18 and topic_selector.is_romantic(" ".join([doc.title, *(e.text for e in entries)])))
        )  # fmt: skip

    async def concern_offer(
        self, user_id: uuid.UUID, character_id: str, now: datetime, today: date, under_18: bool = False
    ) -> TopicOffer | None:
        """負の懸念(病気・不調・別れなど)の「その後」を、軽く聞く候補(topic-timeline-memory.md の Q1 の案 b)。

        ユーザーが2日以上、自分から話した懸念に限り、最後に話してから14日後(30日まで)に、1回だけ。
        本人の健康以外のセンシティブな内容(信条、第三者のことなど)は、聞かない。自発発話でだけ使う。
        """
        settings = self._settings
        docs = await self._current(user_id, character_id)
        muted = self.muted_by_relation(docs)
        best: TopicOffer | None = None
        for doc in docs:
            if self._blocked(doc, muted, under_18, allow_negative=True):
                continue
            entries = [e for e in doc.entries if e.active]
            if any(e.sensitivity not in (None, "", "health") for e in entries):
                continue
            latest = max(entries, key=lambda e: e.told_on)
            if (latest.valence or 0.0) >= topic_selector.NEGATIVE_VALENCE:
                continue  # 懸念ではない(ふつうの話題・予定は、ほかの候補で扱う)
            if len({e.told_on for e in entries}) < settings.proactive_concern_min_mentions:
                continue
            days = (today - latest.told_on).days
            if not settings.proactive_concern_after_days <= days <= settings.proactive_concern_window_days:
                continue
            key = f"concern:{latest.key}"
            if key in doc.followup_keys:
                continue  # 同じ懸念について聞くのは、1回だけ
            score = 1.0 - days / (settings.proactive_concern_window_days + 1)  # 間が空くほど、低い
            if best is None or score > best.score:
                text = (
                    f"{doc.title}: 「{latest.text}」と話していた。その後どうなったか、まだ聞いていない。"
                    "聞くなら、1回だけ、そっと気にかける一言で(詳しく聞き出さない。答えなくてもよい形で。"
                    "日付や回数は言わない。励ましや助言を並べない)"
                )
                best = TopicOffer(doc.doc_id, doc.short_id, doc.title, "concern", round(score, 4), text, key)
        return best

    async def similar_offer(
        self, user_id: uuid.UUID, character_id: str, now: datetime, under_18: bool = False
    ) -> TopicOffer | None:
        """類似による連想: 最近ユーザーが話したトピックに、埋め込みが似ているトピックへ移る(自発発話でだけ。Q16)。

        明言された関係でつながっていない行き先だけを対象にする(つながっているものは、関係をたどる連想で扱う)。
        """
        settings = self._settings
        docs = await self._current(user_id, character_id)
        muted = self.muted_by_relation(docs)
        talked = [d for d in docs if d.established and not d.quarantined and d.entries]
        if not talked:
            return None
        focus = max(talked, key=lambda d: max(e.told_on for e in d.entries))
        targets = {r.to for r in focus.relations}
        related = {d.doc_id for d in docs if d.path in targets or any(r.to == focus.path for r in d.relations)}
        cooldown = timedelta(days=settings.proactive_mention_cooldown_days)
        best: TopicOffer | None = None
        for doc in docs:
            if doc.doc_id == focus.doc_id or doc.doc_id in related or self._blocked(doc, muted, under_18):
                continue
            if any(e.sensitivity for e in doc.entries if e.active):
                continue
            mentioned = doc.last_mentioned_by_character_at
            if mentioned and now - mentioned < cooldown:
                continue
            similarity = scoring.cosine(focus.embedding, doc.embedding)
            if similarity < settings.proactive_similar_min:
                continue
            freshness = 1.0 if mentioned is None else 1.0 - 2.0 ** (-max(0, (now - mentioned).days) / 14.0)
            score = (
                topic_selector.B1_PREFERENCE * doc.preference
                + topic_selector.B2_ACTIVATION * scoring.clip(doc.activation / scoring.ACTIVATION_NORMALIZER, 0.0, 1.0)
                + topic_selector.B3_RELEVANCE * similarity
                + topic_selector.B4_FRESHNESS * freshness
                + topic_selector.B5_EMOTION_FIT * 0.5
            )
            if score >= topic_selector.MIN_SCORE and (best is None or score > best.score):
                latest = max((e for e in doc.entries if e.active), key=lambda e: e.told_on)
                text = f"(似た話: {focus.title} → {doc.title}){doc.title}: {doc.description or latest.text}"
                best = TopicOffer(
                    doc.doc_id, doc.short_id, doc.title, "similar", round(score, 4), text,
                    path=f"{focus.title} → {doc.title}", strength=round(similarity, 4),
                )  # fmt: skip
        return best

    # ------------------------------------------------------------------
    # 第3段階: 関係をたどる連想
    # ------------------------------------------------------------------

    async def associate(
        self,
        user_id: uuid.UUID,
        character_id: str,
        focus_id: uuid.UUID,
        axes: dict[str, float] | None,
        values: association.AssocParams,
        visited: set[uuid.UUID],
        now: datetime,
        under_18: bool = False,
    ) -> TopicOffer | None:
        """焦点のトピックから、関係をたどって、次に移る話題を1件選ぶ。なければ None。

        除外(候補・休止・「持ち出さないで」とその伝播・隔離・センシティブ・つらい話・18歳未満の恋愛的な内容・
        最近キャラクタが持ち出したもの・このセッションで話したもの)は、終点にも、経由点にもしない。
        行き先は、キャラクタの好みに合うものほど選ばれやすい(連想の個性)。
        """
        settings = self._settings
        docs = await self._current(user_id, character_id)
        focus = next((d for d in docs if d.doc_id == focus_id), None)
        if focus is None:
            return None
        muted = self.muted_by_relation(docs)
        cooldown = timedelta(days=settings.proactive_mention_cooldown_days)
        blocked: set[uuid.UUID] = set()
        for doc in docs:
            entries = [e for e in doc.entries if e.active]
            latest = max(entries, key=lambda e: e.told_on, default=None)
            mentioned = doc.last_mentioned_by_character_at
            if (
                not doc.established or doc.lifecycle != "active" or doc.doc_id in muted or doc.quarantined
                or latest is None or any(e.sensitivity for e in entries)
                or (latest.valence or 0.0) < topic_selector.NEGATIVE_VALENCE
                or (under_18 and topic_selector.is_romantic(" ".join([doc.title, *(e.text for e in entries)])))
            ):  # fmt: skip
                blocked.add(doc.doc_id)
        best: TopicOffer | None = None
        for found in association.spread(focus, docs, blocked, axes, values):
            doc = found.doc
            mentioned = doc.last_mentioned_by_character_at
            if doc.doc_id in visited or (mentioned and now - mentioned < cooldown):
                continue  # 終点にしない(経由は、してよい: このセッションで話した話題を通って、その先へ移れる)
            freshness = 1.0 if mentioned is None else 1.0 - 2.0 ** (-max(0, (now - mentioned).days) / 14.0)
            score = (
                topic_selector.B1_PREFERENCE * doc.preference
                + topic_selector.B2_ACTIVATION * scoring.clip(doc.activation / scoring.ACTIVATION_NORMALIZER, 0.0, 1.0)
                + topic_selector.B4_FRESHNESS * freshness
                + topic_selector.B5_EMOTION_FIT * 0.5
                + settings.association_weight * found.strength
            )
            if best is None or score > best.score:
                latest = max((e for e in doc.entries if e.active), key=lambda e: e.told_on)
                text = f"(つながり: {found.path}){doc.title}: {doc.description or latest.text}"
                if doc.description and latest.text not in doc.description:
                    text += f" 最近: {latest.text}"
                best = TopicOffer(
                    doc.doc_id, doc.short_id, doc.title, "associate", round(score, 4), text,
                    path=found.path, strength=round(found.strength, 4),
                )  # fmt: skip
        return best

    async def _refresh_activation(self, user_id: uuid.UUID, character_id: str, doc: TopicDoc, today: date) -> None:
        events = await self._store.recalls(user_id, character_id, doc.doc_id)
        doc.activation = round(scoring.activation(events, today, doc.preference, self._settings), 4)

    async def record_offer_used(
        self,
        user_id: uuid.UUID,
        character_id: str,
        offer: TopicOffer,
        session_id: uuid.UUID,
        turn_id: str,
        today: date,
        now: datetime,
    ) -> None:
        """キャラクタが、トピックを自分から持ち出した。ユーザーの反応で確定するまで、想起としては数えない。"""
        settings = self._settings
        doc = await self._store.get(user_id, character_id, offer.doc_id)
        if doc is None:
            return
        pending = settings.recall_character_requires_engagement
        await self._store.add_recall(
            user_id, character_id,
            RecallEvent(
                memory_id=doc.doc_id, day=today, trigger="character",
                source="followup" if offer.kind in ("followup", "concern") else "topic_selection",
                weight=settings.recall_weight_character, status="pending" if pending else "confirmed",
                ts=now, session_id=session_id, turn_id=turn_id,
            ),
        )  # fmt: skip
        doc.last_mentioned_by_character_at = doc.updated_at = now
        if offer.kind in ("followup", "concern") and offer.entry_key:
            doc.last_followup_at = now
            doc.followup_keys = [*doc.followup_keys, offer.entry_key]  # 同じ予定について聞くのは、1回だけ
        if not pending:
            await self._refresh_activation(user_id, character_id, doc, today)
        await self._store.save(user_id, character_id, doc, "usage")

    async def record_user_recall(
        self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID, session_id: uuid.UUID, turn_id: str,
        today: date, now: datetime,
    ) -> None:  # fmt: skip
        """ユーザーが、その話題に触れた(経過をプロンプトに入れた)。トピックの想起として数える。

        根拠の記憶の想起としては、数えない(中期の期限は延びず、昇格の判定にも使わない)。
        """
        doc = await self._store.get(user_id, character_id, doc_id)
        if doc is None:
            return
        event = RecallEvent(
            memory_id=doc_id, day=today, trigger="user", source="search", weight=self._settings.recall_weight_user,
            ts=now, session_id=session_id, turn_id=turn_id,
        )  # fmt: skip
        if await self._store.add_recall(user_id, character_id, event):
            await self._refresh_activation(user_id, character_id, doc, today)
            await self._store.save(user_id, character_id, doc, "usage")

    async def resolve_pending(
        self, user_id: uuid.UUID, character_id: str, query: list[float] | None, valence: float, today: date, now: datetime
    ) -> dict[str, int]:
        """キャラクタ起点のトピックの想起を、次のユーザー発話で確定または破棄する(記憶と同じ規則)。"""
        result = {"confirmed": 0, "discarded": 0}
        for event in await self._store.recalls(user_id, character_id):
            if event.status != "pending":
                continue
            doc = await self._store.get(user_id, character_id, event.memory_id)
            engaged = (
                doc is not None and valence > -0.5 and scoring.cosine(query, doc.embedding) >= scoring.MIN_SIMILARITY
            )
            status = "confirmed" if engaged else "discarded"
            await self._store.set_recall_status(user_id, character_id, event, status, now)
            result[status] += 1
            if engaged and doc is not None:
                await self._refresh_activation(user_id, character_id, doc, today)
                await self._store.save(user_id, character_id, doc, "usage")
        return result

    async def character_mentions(self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID) -> int:
        """このセッションで、キャラクタがトピックを自分から持ち出した回数(持ち出しの上限は、記憶と共有する)。"""
        events = await self._store.recalls(user_id, character_id)
        return sum(1 for e in events if e.trigger == "character" and e.session_id == session_id)
