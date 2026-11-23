"""トピックの文書の保存先(PostgreSQL。本体仕様書 §9.13、§12、§13.4.6)。

- すべての操作を `(user_id, character_id)` のスコープの中で行う。分離は、行レベルセキュリティが強制する。
- 文書(frontmatter と本文)が正。出典と項目の索引は、文書を保存するたびに、同じトランザクションで作り直す。
- アーカイブ(`topic_archive`)は、アプリのロールに SELECT がない。読む・直す・消すは、専用の関数を通す。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from mycompanion.config import EngineSettings
from mycompanion.db.memory_store import _parse_vector, _vector
from mycompanion.db.postgres import Database, Scope
from mycompanion.memory import okf
from mycompanion.memory.models import RecallEvent
from mycompanion.memory.okf import Entry, Source, TopicArchive, TopicDoc
from mycompanion.memory.timeparse import Occurred

_DOC_COLUMNS = (
    "doc_id, path, okf_type, frontmatter, body, lifecycle, established_at, stale_after, muted, quarantined, "
    "embedding::text, next_entry_no, version, created_day, created_at, updated_at, "
    "preference, activation, last_mentioned_by_character_at, last_followup_at, followup_keys"
)


def _moment(text: Any) -> datetime | None:
    return datetime.fromisoformat(str(text)) if text else None


def _doc(row: tuple[Any, ...], sources: list[tuple[Any, ...]], entries: list[tuple[Any, ...]]) -> TopicDoc:
    front: dict[str, Any] = row[3]
    generated = front.get("generated") or {}
    texts = okf.parse_body(row[4])  # 項目の内容は、本文が正
    doc = TopicDoc(
        doc_id=row[0], path=row[1], okf_type=row[2],
        title=str(front.get("title", "")), description=str(front.get("description", "")),
        tags=list(front.get("tags") or []), aliases=list(front.get("aliases") or []),
        status=str(front.get("status", "draft")),
        generated_by=str(generated.get("by", "")), generated_at=_moment(generated.get("at")),
        verified=list(front.get("verified") or []),
        lifecycle=row[5], established_at=row[6], stale_after=row[7], muted=row[8], quarantined=row[9],
        embedding=_parse_vector(row[10]), next_entry_no=row[11], version=row[12], created_day=row[13],
        created_at=row[14], updated_at=row[15], preference=float(row[16]), activation=float(row[17]),
        last_mentioned_by_character_at=row[18], last_followup_at=row[19], followup_keys=list(row[20] or []),
    )  # fmt: skip
    doc.relations = okf.relations_from_frontmatter(front)
    doc.sources = [Source(key=s[0], memory_id=s[1], told_on=s[2], last_modified=s[3], expired=s[4]) for s in sources]
    for e in entries:
        text = texts.get(e[0], {}).get("text", "")
        doc.entries.append(
            Entry(
                key=e[0], kind=e[1], text=text, occurred=Occurred(e[2], e[3], e[4]), expression=e[5],
                told_at=e[6], told_on=e[7], superseded_by=e[8], resolves=e[9],
                valence=float(e[10]) if e[10] is not None else None, sensitivity=e[11], quarantined=e[12],
                source_keys=list(e[13]),
            )
        )  # fmt: skip
    doc.entries.sort(key=lambda entry: int(entry.key[1:]))
    return doc


def _archive(row: tuple[Any, ...]) -> TopicArchive:
    front: dict[str, Any] = row[3]
    body = str(row[4])
    summary = body.split("\n\n", 1)[1].strip() if "\n\n" in body else ""
    return TopicArchive(
        archive_id=row[0], original_doc_id=row[1], path=row[2], title=str(front.get("title", "")),
        summary="" if summary.startswith("(概要はありません") else summary,
        summary_state=row[5], pending_lines=[str(x) for x in (row[6] or [])], memory_ids=list(row[7] or []),
        first_occurred=row[8], last_occurred=row[9], archived_at=row[10],
    )  # fmt: skip


class PgTopicStore:
    def __init__(self, database: Database, settings: EngineSettings) -> None:
        self._db = database
        self._settings = settings

    def _tx(self, user_id: uuid.UUID, character_id: str):  # type: ignore[no-untyped-def]
        return self._db.scoped_transaction(Scope(user_id, character_id))

    async def save(
        self, user_id: uuid.UUID, character_id: str, doc: TopicDoc, change_kind: str, target_day: date | None = None
    ) -> None:
        front, body = Jsonb(doc.frontmatter()), doc.body()
        vector = _vector(doc.embedding)
        dimensions = len(doc.embedding) if doc.embedding else None
        async with self._tx(user_id, character_id) as conn:
            previous = await (
                await conn.execute(
                    "SELECT version, frontmatter, body FROM topic_doc WHERE doc_id = %s FOR UPDATE", (doc.doc_id,)
                )
            ).fetchone()
            if previous is None:
                await conn.execute(
                    "INSERT INTO topic_doc (doc_id, user_id, character_id, path, okf_type, frontmatter, body, lifecycle, "
                    "established_at, stale_after, muted, quarantined, embedding, embedding_model, embedding_dimensions, "
                    "mc_profile, next_entry_no, version, created_day, created_at, updated_at, preference, activation, "
                    "last_mentioned_by_character_at, last_followup_at, followup_keys) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s, %s, "
                    "coalesce(%s, now()), coalesce(%s, now()), %s, %s, %s, %s, %s)",
                    (
                        doc.doc_id, user_id, character_id, doc.path, doc.okf_type, front, body, doc.lifecycle,
                        doc.established_at, doc.stale_after, doc.muted, doc.quarantined, vector,
                        self._settings.embedding_model, dimensions, okf.PROFILE_VERSION, doc.next_entry_no,
                        doc.version, doc.created_day, doc.created_at, doc.updated_at, doc.preference,
                        doc.activation, doc.last_mentioned_by_character_at, doc.last_followup_at,
                        list(doc.followup_keys),
                    ),
                )  # fmt: skip
            else:
                if change_kind != "usage":  # 運用の値だけの保存は、改訂履歴に残さず、版も上げない
                    await self._revise(conn, user_id, character_id, doc, previous, change_kind, target_day)
                else:
                    doc.version = previous[0]
                await conn.execute(
                    "UPDATE topic_doc SET frontmatter = %s, body = %s, lifecycle = %s, established_at = %s, "
                    "stale_after = %s, muted = %s, quarantined = %s, embedding = %s::vector, embedding_model = %s, "
                    "embedding_dimensions = %s, next_entry_no = %s, version = %s, updated_at = coalesce(%s, now()), "
                    "preference = %s, activation = %s, last_mentioned_by_character_at = %s, last_followup_at = %s, "
                    "followup_keys = %s WHERE doc_id = %s",
                    (
                        front, body, doc.lifecycle, doc.established_at, doc.stale_after, doc.muted, doc.quarantined,
                        vector, self._settings.embedding_model, dimensions, doc.next_entry_no, doc.version,
                        doc.updated_at, doc.preference, doc.activation, doc.last_mentioned_by_character_at,
                        doc.last_followup_at, list(doc.followup_keys), doc.doc_id,
                    ),
                )  # fmt: skip
            # 派生の索引を作り直す(文書と同じトランザクション)
            await conn.execute("DELETE FROM topic_doc_source WHERE doc_id = %s", (doc.doc_id,))
            await conn.execute("DELETE FROM topic_entry WHERE doc_id = %s", (doc.doc_id,))
            for source in doc.sources:
                await conn.execute(
                    "INSERT INTO topic_doc_source (user_id, character_id, doc_id, source_key, memory_id, told_on, "
                    "last_modified, expired) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        user_id, character_id, doc.doc_id, source.key, source.memory_id, source.told_on,
                        source.last_modified, source.expired,
                    ),
                )  # fmt: skip
            for entry in doc.entries:
                await conn.execute(
                    "INSERT INTO topic_entry (user_id, character_id, doc_id, entry_key, entry_kind, occurred_from, "
                    "occurred_to, occurred_precision, time_expression, told_at, told_on, superseded_by, resolves, "
                    "valence, sensitivity, quarantined, source_keys) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        user_id, character_id, doc.doc_id, entry.key, entry.kind, entry.occurred.start,
                        entry.occurred.end, entry.occurred.precision, entry.expression, entry.told_at, entry.told_on,
                        entry.superseded_by, entry.resolves, entry.valence, entry.sensitivity, entry.quarantined,
                        list(entry.source_keys),
                    ),
                )  # fmt: skip

    @staticmethod
    async def _revise(
        conn: Any, user_id: uuid.UUID, character_id: str, doc: TopicDoc, previous: tuple[Any, ...],
        change_kind: str, target_day: date | None,
    ) -> None:  # fmt: skip
        """前の版を、改訂履歴に残す。版を1つ上げる。"""
        await conn.execute(
            "INSERT INTO topic_doc_revision (user_id, character_id, doc_id, version, frontmatter, body, "
            "change_kind, target_day) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (doc_id, version) DO NOTHING",
            (user_id, character_id, doc.doc_id, previous[0], Jsonb(previous[1]), previous[2], change_kind, target_day),
        )
        doc.version = previous[0] + 1

    async def _load(self, conn: Any, condition: str, params: tuple[Any, ...]) -> list[TopicDoc]:
        rows = await (
            await conn.execute(f"SELECT {_DOC_COLUMNS} FROM topic_doc WHERE {condition} ORDER BY created_at, path", params)
        ).fetchall()
        if not rows:
            return []
        ids = [row[0] for row in rows]
        sources: dict[uuid.UUID, list[tuple[Any, ...]]] = {}
        for row in await (
            await conn.execute(
                "SELECT doc_id, source_key, memory_id, told_on, last_modified, expired FROM topic_doc_source "
                "WHERE doc_id = ANY(%s) ORDER BY told_on NULLS LAST, source_key",
                (ids,),
            )
        ).fetchall():
            sources.setdefault(row[0], []).append(row[1:])
        entries: dict[uuid.UUID, list[tuple[Any, ...]]] = {}
        for row in await (
            await conn.execute(
                "SELECT doc_id, entry_key, entry_kind, occurred_from, occurred_to, occurred_precision, time_expression, "
                "told_at, told_on, superseded_by, resolves, valence, sensitivity, quarantined, source_keys "
                "FROM topic_entry WHERE doc_id = ANY(%s)",
                (ids,),
            )
        ).fetchall():
            entries.setdefault(row[0], []).append(row[1:])
        return [_doc(row, sources.get(row[0], []), entries.get(row[0], [])) for row in rows]

    async def get(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> TopicDoc | None:
        async with self._tx(user_id, character_id) as conn:
            docs = await self._load(conn, "doc_id = %s", (doc_id,))
        return docs[0] if docs else None

    async def list(self, user_id: uuid.UUID, character_id: str) -> list[TopicDoc]:
        async with self._tx(user_id, character_id) as conn:
            return await self._load(conn, "true", ())

    async def delete(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute("DELETE FROM topic_doc WHERE doc_id = %s", (doc_id,))  # 索引と履歴は、連鎖で消える

    async def drop_revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute("DELETE FROM topic_doc_revision WHERE doc_id = %s", (doc_id,))

    async def prune_revisions(self, user_id: uuid.UUID, character_id: str, keep: int, before: datetime) -> int:
        async with self._tx(user_id, character_id) as conn:
            cursor = await conn.execute(
                "DELETE FROM topic_doc_revision r WHERE r.changed_at < %s AND r.version NOT IN ("
                "SELECT version FROM topic_doc_revision k WHERE k.doc_id = r.doc_id ORDER BY version DESC LIMIT %s)",
                (before, keep),
            )
            return int(cursor.rowcount or 0)

    async def revisions(self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(
                    "SELECT version, frontmatter, body, change_kind, target_day, changed_at FROM topic_doc_revision "
                    "WHERE doc_id = %s ORDER BY version",
                    (doc_id,),
                )
            ).fetchall()
        keys = ("version", "frontmatter", "body", "change_kind", "target_day", "changed_at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # --- トピックの想起(記憶の想起とは、別に数える) ---

    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        async with self._tx(user_id, character_id) as conn:
            cursor = await conn.execute(
                "INSERT INTO topic_recall_events (user_id, character_id, doc_id, session_id, turn_id, ts, day, trigger, "
                "source, weight, status, confirmed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (doc_id, day, trigger) DO NOTHING",
                (
                    user_id, character_id, event.memory_id, event.session_id, event.turn_id, event.ts, event.day,
                    event.trigger, event.source, event.weight, event.status,
                    event.ts if event.status == "confirmed" else None,
                ),
            )  # fmt: skip
            return cursor.rowcount > 0

    async def recalls(
        self, user_id: uuid.UUID, character_id: str, doc_id: uuid.UUID | None = None
    ) -> list[RecallEvent]:
        condition, params = ("WHERE doc_id = %s", (doc_id,)) if doc_id is not None else ("", ())
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(
                    "SELECT doc_id, day, trigger, source, weight, status, ts, session_id, turn_id "
                    f"FROM topic_recall_events {condition} ORDER BY ts",
                    params,
                )
            ).fetchall()
        return [
            RecallEvent(r[0], r[1], r[2], r[3], float(r[4]), r[5], r[6], r[7], r[8]) for r in rows
        ]

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "UPDATE topic_recall_events SET status = %s, confirmed_at = %s "
                "WHERE doc_id = %s AND day = %s AND trigger = %s",
                (status, now if status == "confirmed" else None, event.memory_id, event.day, event.trigger),
            )

    # --- アーカイブ(読む・直す・消すは、専用の関数を通す) ---

    async def add_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "INSERT INTO topic_archive (archive_id, user_id, character_id, original_doc_id, path, frontmatter, body, "
                "summary_state, pending_json, memory_ids, first_occurred, last_occurred, archived_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    archive.archive_id, user_id, character_id, archive.original_doc_id, archive.path,
                    Jsonb(archive.frontmatter()), archive.body(), archive.summary_state,
                    Jsonb(archive.pending_lines), list(archive.memory_ids), archive.first_occurred,
                    archive.last_occurred, archive.archived_at,
                ),
            )  # fmt: skip

    async def archives(self, user_id: uuid.UUID, character_id: str) -> list[TopicArchive]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (await conn.execute("SELECT * FROM list_my_topic_archives()")).fetchall()
        return [_archive(row) for row in rows]

    async def update_archive(self, user_id: uuid.UUID, character_id: str, archive: TopicArchive) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "SELECT update_my_topic_archive(%s, %s, %s, %s, %s, %s)",
                (
                    archive.archive_id, Jsonb(archive.frontmatter()), archive.body(), archive.summary_state,
                    Jsonb(archive.pending_lines), list(archive.memory_ids),
                ),
            )  # fmt: skip

    async def purge_archive(self, user_id: uuid.UUID, character_id: str, archive_id: uuid.UUID) -> bool:
        async with self._tx(user_id, character_id) as conn:
            row = await (await conn.execute("SELECT purge_my_topic_archive(%s)", (archive_id,))).fetchone()
        return bool(row and row[0])
