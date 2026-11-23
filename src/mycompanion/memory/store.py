"""記憶の保存先の抽象と、メモリ上の実装(本体仕様書 §9、§13.4.6)。

**すべての操作に `user_id` と `character_id` を渡す。** ほかのユーザーはもちろん、同じユーザーの別のキャラクタの
記憶も、検索・想起・話題選択・整理の対象にしない。本番の実装は `mycompanion.db.memory_store`(PostgreSQL + pgvector)。

例外は、スケジューラが整理の対象を見つける処理(`consolidation_targets`、`idle_sessions`)だけ。
返すのは ID と日付だけで、記憶の内容は返さない。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol

from mycompanion.memory.models import Memory, RecallEvent, RunRecord, WorkingItem
from mycompanion.memory.scoring import cosine

Key = tuple[uuid.UUID, str]


@dataclass(frozen=True)
class Target:
    """整理の対象(スケジューラ用)。内容は含めない。"""

    user_id: uuid.UUID
    character_id: str
    timezone: str
    oldest_day: date | None = None


class MemoryStore(Protocol):
    async def add(self, user_id: uuid.UUID, character_id: str, memory: Memory) -> Memory: ...

    async def get(
        self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, include_deleted: bool = False
    ) -> Memory | None: ...

    async def list(
        self, user_id: uuid.UUID, character_id: str, tier: str | None = None, kind: str | None = None
    ) -> list[Memory]:
        """削除されていない記憶(隔離されたものを含む。利用者が確認・削除できるように)。"""
        ...

    async def update(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, **fields: Any) -> None: ...

    async def soft_delete(
        self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID], now: datetime
    ) -> int: ...

    async def hard_delete(self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID]) -> int:
        """行を消す(短期から中期へ「移す」ときの、短期の削除)。"""
        ...

    async def candidates(
        self, user_id: uuid.UUID, character_id: str, query: list[float], tier: str, limit: int
    ) -> list[tuple[Memory, float]]:
        """検索の候補(記憶, 意味類似度)。隔離・削除された記憶は含めない。類似度の高い順。"""
        ...

    # --- 想起(§9.9) ---
    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        """想起を記録する。同じ(記憶, 日, 起点)がすでにあれば、何もしないで False。"""
        ...

    async def recalls(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID) -> list[RecallEvent]: ...

    async def pending_recalls(self, user_id: uuid.UUID, character_id: str) -> list[RecallEvent]:
        """確定を待っている、キャラクタ起点の想起。"""
        ...

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None: ...

    async def count_character_recalls(self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID) -> int:
        """このセッションで、キャラクタが記憶を持ち出した回数(§8.7 の上限の判定)。"""
        ...

    # --- ワーキングセット(§9.9.3) ---
    async def working_set(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[WorkingItem]: ...

    async def put_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_id: uuid.UUID, turn_no: int, ttl: int
    ) -> None: ...

    async def remove_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_ids: list[uuid.UUID] | None = None
    ) -> None:
        """ワーキングセットから外す。`memory_ids` を省略すると、セッションの分をすべて消す。"""
        ...

    # --- 整理の記録、baseline、削除の記録 ---
    async def add_run(self, user_id: uuid.UUID, character_id: str, run: RunRecord) -> None: ...

    async def runs(self, user_id: uuid.UUID, character_id: str, run_type: str | None = None) -> list[RunRecord]: ...

    async def set_baseline(
        self, user_id: uuid.UUID, character_id: str, vad: dict[str, float], categories: dict[str, float],
        sample_sessions: int, sample_days: int, now: datetime,
    ) -> None: ...  # fmt: skip

    async def count_sessions(self, user_id: uuid.UUID, character_id: str) -> int: ...

    async def record_deletion(
        self, user_id: uuid.UUID, scope: str, status: str, details: dict[str, Any], now: datetime
    ) -> None: ...

    # --- スケジューラ用(内容は返さない) ---
    async def consolidation_targets(self) -> list[Target]:
        """短期記憶を持つ(user, character)と、その最も古い日。"""
        ...

    async def all_scopes(self) -> list[Target]:
        """記憶を持つ(user, character)の一覧(月次整理と、期限切れの削除の対象)。"""
        ...


class InMemoryMemoryStore:
    """メモリ上の実装。スコープの分離も再現する。"""

    def __init__(self) -> None:
        self.items: dict[Key, dict[uuid.UUID, Memory]] = {}
        self.recall_events: dict[Key, list[RecallEvent]] = {}
        self.working: dict[tuple[uuid.UUID, str, uuid.UUID], dict[uuid.UUID, WorkingItem]] = {}
        self.run_records: dict[Key, list[RunRecord]] = {}
        self.baselines: dict[Key, dict[str, Any]] = {}
        self.session_counts: dict[Key, int] = {}
        self.deletions: list[dict[str, Any]] = []
        self.timezones: dict[uuid.UUID, str] = {}

    def _scope(self, user_id: uuid.UUID, character_id: str) -> dict[uuid.UUID, Memory]:
        return self.items.setdefault((user_id, character_id), {})

    async def add(self, user_id: uuid.UUID, character_id: str, memory: Memory) -> Memory:
        self._scope(user_id, character_id)[memory.memory_id] = memory
        return memory

    async def get(
        self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, include_deleted: bool = False
    ) -> Memory | None:
        memory = self._scope(user_id, character_id).get(memory_id)
        if memory is None or (memory.deleted_at is not None and not include_deleted):
            return None
        return memory

    async def list(
        self, user_id: uuid.UUID, character_id: str, tier: str | None = None, kind: str | None = None
    ) -> list[Memory]:
        return [
            m
            for m in self._scope(user_id, character_id).values()
            if m.deleted_at is None and (tier is None or m.tier == tier) and (kind is None or m.kind == kind)
        ]

    async def update(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, **fields: Any) -> None:
        scope = self._scope(user_id, character_id)
        if memory_id in scope:
            scope[memory_id] = replace(scope[memory_id], **fields)

    async def soft_delete(
        self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID], now: datetime
    ) -> int:
        scope, count = self._scope(user_id, character_id), 0
        for memory_id in memory_ids:
            if memory_id in scope and scope[memory_id].deleted_at is None:
                # 削除した記憶の本文と埋め込みは、残さない
                scope[memory_id] = replace(scope[memory_id], deleted_at=now, summary="", embedding=None, topics=[])
                count += 1
        for (uid, cid, _), items in self.working.items():
            if (uid, cid) == (user_id, character_id):
                for memory_id in memory_ids:
                    items.pop(memory_id, None)
        return count

    async def hard_delete(self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID]) -> int:
        scope = self._scope(user_id, character_id)
        return sum(1 for memory_id in memory_ids if scope.pop(memory_id, None) is not None)

    async def candidates(
        self, user_id: uuid.UUID, character_id: str, query: list[float], tier: str, limit: int
    ) -> list[tuple[Memory, float]]:
        found = [
            (m, cosine(query, m.embedding))
            for m in self._scope(user_id, character_id).values()
            if m.tier == tier and m.searchable
        ]
        found.sort(key=lambda pair: pair[1], reverse=True)
        return found[:limit]

    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        events = self.recall_events.setdefault((user_id, character_id), [])
        if any((e.memory_id, e.day, e.trigger) == (event.memory_id, event.day, event.trigger) for e in events):
            return False
        events.append(event)
        return True

    async def recalls(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID) -> list[RecallEvent]:
        return [e for e in self.recall_events.get((user_id, character_id), []) if e.memory_id == memory_id]

    async def pending_recalls(self, user_id: uuid.UUID, character_id: str) -> list[RecallEvent]:
        return [e for e in self.recall_events.get((user_id, character_id), []) if e.status == "pending"]

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None:
        for item in self.recall_events.get((user_id, character_id), []):
            if (item.memory_id, item.day, item.trigger) == (event.memory_id, event.day, event.trigger):
                item.status = status

    async def count_character_recalls(self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID) -> int:
        events = self.recall_events.get((user_id, character_id), [])
        return sum(1 for e in events if e.trigger == "character" and e.session_id == session_id)

    async def working_set(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[WorkingItem]:
        return list(self.working.get((user_id, character_id, session_id), {}).values())

    async def put_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_id: uuid.UUID, turn_no: int, ttl: int
    ) -> None:
        self.working.setdefault((user_id, character_id, session_id), {})[memory_id] = WorkingItem(memory_id, turn_no)

    async def remove_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_ids: list[uuid.UUID] | None = None
    ) -> None:
        key = (user_id, character_id, session_id)
        if memory_ids is None:
            self.working.pop(key, None)
            return
        for memory_id in memory_ids:
            self.working.get(key, {}).pop(memory_id, None)

    async def add_run(self, user_id: uuid.UUID, character_id: str, run: RunRecord) -> None:
        self.run_records.setdefault((user_id, character_id), []).append(run)

    async def runs(self, user_id: uuid.UUID, character_id: str, run_type: str | None = None) -> list[RunRecord]:
        records = self.run_records.get((user_id, character_id), [])
        return [r for r in records if run_type is None or r.run_type == run_type]

    async def set_baseline(
        self, user_id: uuid.UUID, character_id: str, vad: dict[str, float], categories: dict[str, float],
        sample_sessions: int, sample_days: int, now: datetime,
    ) -> None:  # fmt: skip
        self.baselines[(user_id, character_id)] = {
            "vad": vad, "categories": categories, "sample_sessions": sample_sessions, "sample_days": sample_days,
        }  # fmt: skip

    async def count_sessions(self, user_id: uuid.UUID, character_id: str) -> int:
        return self.session_counts.get((user_id, character_id), 0)

    async def record_deletion(
        self, user_id: uuid.UUID, scope: str, status: str, details: dict[str, Any], now: datetime
    ) -> None:
        self.deletions.append({"user_id": user_id, "scope": scope, "status": status, "details": details, "ts": now})

    async def consolidation_targets(self) -> list[Target]:
        targets = []
        for (user_id, character_id), scope in self.items.items():
            days = [m.source_day for m in scope.values() if m.tier == "short" and m.deleted_at is None and m.source_day]
            if days:
                targets.append(Target(user_id, character_id, self.timezones.get(user_id, "Asia/Tokyo"), min(days)))
        return targets

    async def all_scopes(self) -> list[Target]:
        return [
            Target(user_id, character_id, self.timezones.get(user_id, "Asia/Tokyo"))
            for (user_id, character_id), scope in self.items.items()
            if any(m.deleted_at is None for m in scope.values())
        ]
