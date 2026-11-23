"""トピックの文書の保存先(本体仕様書 §9.13、§12)。

本番は PostgreSQL(`db.topic_store.PgTopicStore`)。ここには、インターフェースと、メモリ上の実装を置く。
どの操作も `(user_id, character_id)` のスコープの中で行う(バンドルは、キャラクタごとに別)。

アーカイブ(削除されたトピックの概要)は、**生成の経路からは読まない**。読むのは、利用者への表示・書き出しと、
削除の伝播(概要の取り下げ)だけ。本番では、アプリのロールに `topic_archive` の SELECT 権限を与えず、専用の関数を通す。
"""

from __future__ import annotations

import copy
import uuid
from datetime import date, datetime
from typing import Any, Protocol

from mycompanion.memory.models import RecallEvent
from mycompanion.memory.okf import TopicArchive, TopicDoc

Key = tuple[uuid.UUID, str]


class TopicStore(Protocol):
    async def save(
        self, user_id: uuid.UUID, character_id: str, doc: TopicDoc, change_kind: str, target_day: date | None = None
    ) -> None:
        """文書を保存する(なければ作る)。前の版を改訂履歴に残し、派生の索引(出典・項目)を作り直す。"""
        ...

    async def get(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> TopicDoc | None: ...

    async def list(self, user_id: uuid.UUID, character_id: str) -> list[TopicDoc]: ...

    async def delete(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        """文書と、派生の索引・改訂履歴を消す。"""
        ...

    async def drop_revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        """改訂履歴を消す(削除した記憶の内容を、履歴に残さない)。"""
        ...

    async def prune_revisions(self, user_id: uuid.UUID, character_id: str, keep: int, before: datetime) -> int:
        """改訂履歴を整理する(文書ごとに直近 `keep` 版、または `before` より新しいものだけ残す)。"""
        ...

    async def revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> list[dict[str, Any]]: ...

    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        """トピックの想起を記録する(`memory_id` に、文書の ID を入れる)。同じ日・同じ起点は1回。記録したら True。"""
        ...

    async def recalls(
        self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID | None = None
    ) -> list[RecallEvent]: ...

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None: ...

    async def add_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None: ...

    async def archives(self, user_id: uuid.UUID, character_id: str) -> list[TopicArchive]: ...

    async def update_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None: ...

    async def purge_archive(self, user_id: uuid.UUID, character_id: str, archive_id: uuid.UUID) -> bool: ...


class InMemoryTopicStore:
    """メモリ上の実装。スコープの分離と、「保存した時点の内容が残る」ことを再現する。"""

    def __init__(self) -> None:
        self.docs: dict[Key, dict[uuid.UUID, TopicDoc]] = {}
        self.history: dict[Key, dict[uuid.UUID, list[dict[str, Any]]]] = {}
        self.archived: dict[Key, dict[uuid.UUID, TopicArchive]] = {}
        self.recall_events: dict[Key, list[RecallEvent]] = {}

    async def save(
        self, user_id: uuid.UUID, character_id: str, doc: TopicDoc, change_kind: str, target_day: date | None = None
    ) -> None:
        scope = self.docs.setdefault((user_id, character_id), {})
        previous = scope.get(doc.doc_id)
        if previous is not None and change_kind == "usage":
            doc.version = previous.version
        elif previous is not None:
            self.history.setdefault((user_id, character_id), {}).setdefault(doc.doc_id, []).append(
                {
                    "version": previous.version, "frontmatter": previous.frontmatter(), "body": previous.body(),
                    "change_kind": change_kind, "target_day": target_day, "changed_at": doc.updated_at,
                }
            )  # fmt: skip
            doc.version = previous.version + 1
        scope[doc.doc_id] = copy.deepcopy(doc)

    async def get(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> TopicDoc | None:
        doc = self.docs.get((user_id, character_id), {}).get(doc_id)
        return copy.deepcopy(doc) if doc else None

    async def list(self, user_id: uuid.UUID, character_id: str) -> list[TopicDoc]:
        docs = self.docs.get((user_id, character_id), {}).values()
        return [copy.deepcopy(doc) for doc in sorted(docs, key=lambda d: (d.created_at or datetime.min, d.path))]

    async def delete(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        self.docs.get((user_id, character_id), {}).pop(doc_id, None)
        self.history.get((user_id, character_id), {}).pop(doc_id, None)
        events = self.recall_events.get((user_id, character_id), [])
        events[:] = [e for e in events if e.memory_id != doc_id]

    async def drop_revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        self.history.get((user_id, character_id), {}).pop(doc_id, None)

    async def prune_revisions(self, user_id: uuid.UUID, character_id: str, keep: int, before: datetime) -> int:
        removed = 0
        for doc_id, items in self.history.get((user_id, character_id), {}).items():
            kept = [
                item for index, item in enumerate(reversed(items))
                if index < keep or (item["changed_at"] is not None and item["changed_at"] >= before)
            ]  # fmt: skip
            removed += len(items) - len(kept)
            self.history[(user_id, character_id)][doc_id] = list(reversed(kept))
        return removed

    async def revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> list[dict[str, Any]]:
        return list(self.history.get((user_id, character_id), {}).get(doc_id, []))

    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        events = self.recall_events.setdefault((user_id, character_id), [])
        if any((e.memory_id, e.day, e.trigger) == (event.memory_id, event.day, event.trigger) for e in events):
            return False
        events.append(copy.copy(event))
        return True

    async def recalls(
        self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID | None = None
    ) -> list[RecallEvent]:
        events = self.recall_events.get((user_id, character_id), [])
        return [copy.copy(e) for e in events if doc_id is None or e.memory_id == doc_id]

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None:
        for item in self.recall_events.get((user_id, character_id), []):
            if (item.memory_id, item.day, item.trigger) == (event.memory_id, event.day, event.trigger):
                item.status = status

    async def add_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None:
        self.archived.setdefault((user_id, character_id), {})[archive.archive_id] = copy.deepcopy(archive)

    async def archives(self, user_id: uuid.UUID, character_id: str) -> list[TopicArchive]:
        items = self.archived.get((user_id, character_id), {}).values()
        return [copy.deepcopy(a) for a in sorted(items, key=lambda a: a.archived_at)]

    async def update_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None:
        scope = self.archived.get((user_id, character_id), {})
        if archive.archive_id in scope:
            scope[archive.archive_id] = copy.deepcopy(archive)

    async def purge_archive(self, user_id: uuid.UUID, character_id: str, archive_id: uuid.UUID) -> bool:
        return self.archived.get((user_id, character_id), {}).pop(archive_id, None) is not None
