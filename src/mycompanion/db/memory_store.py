"""記憶の保存先の本番実装(PostgreSQL + pgvector。本体仕様書 §9.3、§11.5.3、§13.4.6)。

- すべての操作を `(user_id, character_id)` のスコープの中で行う。分離は、行レベルセキュリティが強制する。
- 意味類似度は、pgvector のコサイン距離で計算する(`embedding <=> クエリ`)。
- スケジューラが対象を探す処理だけは、専用の関数(`list_consolidation_targets` など)を使う。
  返すのは ID と日付だけで、記憶の内容は返さない。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mycompanion.config import EngineSettings
from mycompanion.db.postgres import Database, Scope
from mycompanion.memory.models import Memory, RecallEvent, RunRecord, WorkingItem
from mycompanion.memory.store import Target

# Memory の属性 → 列。JSON の列は、(属性, 列) の対応を別に持つ
_PLAIN = (
    "tier", "kind", "summary", "importance", "sensitivity", "pinned", "core_key", "muted", "quarantined", "quarantine_reason",
    "trust_level", "preference", "preference_version", "activation", "origin_memory_id",
    "last_mentioned_by_character_at", "last_reactivated_at", "source_day", "period", "expires_at", "recall_days",
    "user_recall_days", "last_recalled_at", "promoted_from", "promoted_at", "consolidated_at", "stale", "version",
    "created_at", "deleted_at", "recall_excluded",
)  # fmt: skip
_JSON = {
    "topics": "topics_json",
    "emotion_vad": "emotion_vad_json",
    "emotion_categories": "emotion_categories_json",
    "source_event_ids": "source_event_ids",
    "source_memory_ids": "source_memory_ids",
}
_SELECT = (
    "memory_id, " + ", ".join(_PLAIN) + ", " + ", ".join(_JSON.values()) + ", embedding::text"
)


def _vector(values: list[float] | None) -> str | None:
    return None if values is None else "[" + ",".join(f"{v:.7g}" for v in values) + "]"


def _parse_vector(text: str | None) -> list[float] | None:
    return None if not text else [float(part) for part in text.strip("[]").split(",") if part]


def _memory(row: tuple[Any, ...]) -> Memory:
    values: dict[str, Any] = {"memory_id": row[0]}
    for index, name in enumerate(_PLAIN, start=1):
        values[name] = row[index]
    offset = 1 + len(_PLAIN)
    for index, name in enumerate(_JSON):
        value = row[offset + index]
        values[name] = value if value is not None else ([] if name not in ("emotion_vad", "emotion_categories") else None)
    values["embedding"] = _parse_vector(row[offset + len(_JSON)])
    values["importance"] = float(values["importance"])
    values["preference"] = float(values["preference"])
    values["activation"] = float(values["activation"])
    return Memory(**values)


class PgMemoryStore:
    def __init__(self, database: Database, settings: EngineSettings) -> None:
        self._db = database
        self._settings = settings

    def _tx(self, user_id: uuid.UUID, character_id: str):  # type: ignore[no-untyped-def]
        return self._db.scoped_transaction(Scope(user_id, character_id))

    async def add(self, user_id: uuid.UUID, character_id: str, memory: Memory) -> Memory:
        columns = ["memory_id", "user_id", "character_id", *_PLAIN, *_JSON.values(), "embedding"]
        values: list[Any] = [memory.memory_id, user_id, character_id]
        values += [getattr(memory, name) for name in _PLAIN]
        for name in _JSON:
            value = getattr(memory, name)
            values.append(Jsonb(value) if value is not None else None)
        values.append(_vector(memory.embedding))
        placeholders = ", ".join(["%s"] * (len(columns) - 1)) + ", %s::vector"
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                f"INSERT INTO memory ({', '.join(columns)}, embedding_model, embedding_dimensions) "
                f"VALUES ({placeholders}, %s, %s)",
                (*values, self._settings.embedding_model, len(memory.embedding) if memory.embedding else None),
            )
        return memory

    async def get(
        self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, include_deleted: bool = False
    ) -> Memory | None:
        condition = "" if include_deleted else " AND deleted_at IS NULL"
        async with self._tx(user_id, character_id) as conn:
            row = await (
                await conn.execute(f"SELECT {_SELECT} FROM memory WHERE memory_id = %s{condition}", (memory_id,))
            ).fetchone()
        return _memory(row) if row else None

    async def list(
        self, user_id: uuid.UUID, character_id: str, tier: str | None = None, kind: str | None = None
    ) -> list[Memory]:
        sql = f"SELECT {_SELECT} FROM memory WHERE deleted_at IS NULL"
        params: list[Any] = []
        if tier is not None:
            sql += " AND tier = %s"
            params.append(tier)
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        async with self._tx(user_id, character_id) as conn:
            rows = await (await conn.execute(sql + " ORDER BY created_at", params)).fetchall()
        return [_memory(row) for row in rows]

    async def update(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID, **fields: Any) -> None:
        assignments, params = [], []
        for name, value in fields.items():
            # 列名は、ここで決めた固定の一覧からだけ選ぶ(値は、パラメータで渡す)
            if name in _PLAIN:
                assignments.append(f"{name} = %s")
                params.append(value)
            elif name in _JSON:
                assignments.append(f"{_JSON[name]} = %s")
                params.append(Jsonb(value) if value is not None else None)
            elif name == "embedding":
                assignments.append("embedding = %s::vector")
                params.append(_vector(value))
            else:
                raise KeyError(f"更新できない項目です: {name}")
        if not assignments:
            return
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                f"UPDATE memory SET {', '.join(assignments)} WHERE memory_id = %s", (*params, memory_id)
            )

    async def soft_delete(
        self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID], now: datetime
    ) -> int:
        if not memory_ids:
            return 0
        async with self._tx(user_id, character_id) as conn:
            await conn.execute("DELETE FROM working_set WHERE memory_id = ANY(%s)", (memory_ids,))
            # 削除した記憶の本文と埋め込みは、残さない
            cursor = await conn.execute(
                "UPDATE memory SET deleted_at = %s, summary = '', embedding = NULL, topics_json = '[]'::jsonb "
                "WHERE memory_id = ANY(%s) AND deleted_at IS NULL",
                (now, memory_ids),
            )
            return cursor.rowcount

    async def hard_delete(self, user_id: uuid.UUID, character_id: str, memory_ids: list[uuid.UUID]) -> int:
        if not memory_ids:
            return 0
        async with self._tx(user_id, character_id) as conn:
            cursor = await conn.execute("DELETE FROM memory WHERE memory_id = ANY(%s)", (memory_ids,))
            return cursor.rowcount

    async def candidates(
        self, user_id: uuid.UUID, character_id: str, query: list[float], tier: str, limit: int
    ) -> list[tuple[Memory, float]]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(
                    f"SELECT {_SELECT}, 1 - (embedding <=> %s::vector) AS similarity FROM memory "
                    "WHERE tier = %s AND deleted_at IS NULL AND NOT quarantined AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vector(query), tier, _vector(query), limit),
                )
            ).fetchall()
        return [(_memory(row[:-1]), float(row[-1])) for row in rows]

    # --- 想起 ---

    async def add_recall(self, user_id: uuid.UUID, character_id: str, event: RecallEvent) -> bool:
        async with self._tx(user_id, character_id) as conn:
            cursor = await conn.execute(
                "INSERT INTO recall_events (memory_id, user_id, character_id, session_id, turn_id, ts, day, trigger, "
                "source, weight, used_in_response, status, confirmed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s) "
                "ON CONFLICT (memory_id, day, trigger) DO NOTHING",
                (
                    event.memory_id, user_id, character_id, event.session_id, event.turn_id, event.ts, event.day,
                    event.trigger, event.source, event.weight, event.status,
                    event.ts if event.status == "confirmed" else None,
                ),
            )
            return cursor.rowcount > 0

    @staticmethod
    def _recall(row: tuple[Any, ...]) -> RecallEvent:
        return RecallEvent(
            memory_id=row[0], day=row[1], trigger=row[2], source=row[3], weight=float(row[4]), status=row[5],
            ts=row[6], session_id=row[7], turn_id=row[8],
        )  # fmt: skip

    _RECALL_COLUMNS = "memory_id, day, trigger, source, weight, status, ts, session_id, turn_id"

    async def recalls(self, user_id: uuid.UUID, character_id: str, memory_id: uuid.UUID) -> list[RecallEvent]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(
                    f"SELECT {self._RECALL_COLUMNS} FROM recall_events WHERE memory_id = %s ORDER BY ts", (memory_id,)
                )
            ).fetchall()
        return [self._recall(row) for row in rows]

    async def pending_recalls(self, user_id: uuid.UUID, character_id: str) -> list[RecallEvent]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(f"SELECT {self._RECALL_COLUMNS} FROM recall_events WHERE status = 'pending'")
            ).fetchall()
        return [self._recall(row) for row in rows]

    async def set_recall_status(
        self, user_id: uuid.UUID, character_id: str, event: RecallEvent, status: str, now: datetime
    ) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "UPDATE recall_events SET status = %s, confirmed_at = %s "
                "WHERE memory_id = %s AND day = %s AND trigger = %s",
                (status, now if status == "confirmed" else None, event.memory_id, event.day, event.trigger),
            )

    async def count_character_recalls(self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID) -> int:
        async with self._tx(user_id, character_id) as conn:
            row = await (
                await conn.execute(
                    "SELECT count(*) FROM recall_events WHERE trigger = 'character' AND session_id = %s", (session_id,)
                )
            ).fetchone()
        return int(row[0]) if row else 0

    # --- ワーキングセット ---

    async def working_set(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID
    ) -> list[WorkingItem]:
        async with self._tx(user_id, character_id) as conn:
            rows = await (
                await conn.execute(
                    "SELECT memory_id, last_recalled_turn FROM working_set WHERE session_id = %s", (session_id,)
                )
            ).fetchall()
        return [WorkingItem(row[0], int(row[1])) for row in rows]

    async def put_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_id: uuid.UUID, turn_no: int, ttl: int
    ) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "INSERT INTO working_set (user_id, character_id, session_id, memory_id, last_recalled_turn, ttl_turns) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id, memory_id) DO UPDATE SET "
                "last_recalled_turn = EXCLUDED.last_recalled_turn",
                (user_id, character_id, session_id, memory_id, turn_no, ttl),
            )

    async def remove_working(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, memory_ids: list[uuid.UUID] | None = None
    ) -> None:
        async with self._tx(user_id, character_id) as conn:
            if memory_ids is None:
                await conn.execute("DELETE FROM working_set WHERE session_id = %s", (session_id,))
            else:
                await conn.execute(
                    "DELETE FROM working_set WHERE session_id = %s AND memory_id = ANY(%s)", (session_id, memory_ids)
                )

    # --- 整理の記録、baseline、削除の記録 ---

    async def add_run(self, user_id: uuid.UUID, character_id: str, run: RunRecord) -> None:
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "INSERT INTO memory_consolidation_runs (user_id, character_id, run_type, target, started_at, "
                "finished_at, status, counts_json, model_id, prompt_version, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    user_id, character_id, run.run_type, run.target, run.started_at, run.finished_at, run.status,
                    Jsonb(run.counts), run.model_id, run.prompt_version, run.error,
                ),
            )

    async def runs(self, user_id: uuid.UUID, character_id: str, run_type: str | None = None) -> list[RunRecord]:
        sql = (
            "SELECT run_type, target, status, counts_json, started_at, finished_at, model_id, prompt_version, error "
            "FROM memory_consolidation_runs"
        )
        params: list[Any] = []
        if run_type is not None:
            sql += " WHERE run_type = %s"
            params.append(run_type)
        async with self._tx(user_id, character_id) as conn:
            rows = await (await conn.execute(sql + " ORDER BY started_at", params)).fetchall()
        return [RunRecord(*row) for row in rows]

    async def set_baseline(
        self, user_id: uuid.UUID, character_id: str, vad: dict[str, float], categories: dict[str, float],
        sample_sessions: int, sample_days: int, now: datetime,
    ) -> None:  # fmt: skip
        async with self._tx(user_id, character_id) as conn:
            await conn.execute(
                "INSERT INTO user_state (user_id, character_id, layer, vad_json, categories_json, sample_sessions, "
                "sample_days, updated_at) VALUES (%s, %s, 'baseline', %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, character_id, layer) DO UPDATE SET vad_json = EXCLUDED.vad_json, "
                "categories_json = EXCLUDED.categories_json, sample_sessions = EXCLUDED.sample_sessions, "
                "sample_days = EXCLUDED.sample_days, updated_at = EXCLUDED.updated_at",
                (user_id, character_id, Jsonb(vad), Jsonb(categories), sample_sessions, sample_days, now),
            )

    async def count_sessions(self, user_id: uuid.UUID, character_id: str) -> int:
        async with self._tx(user_id, character_id) as conn:
            row = await (await conn.execute("SELECT count(*) FROM sessions")).fetchone()
        return int(row[0]) if row else 0

    async def record_deletion(
        self, user_id: uuid.UUID, scope: str, status: str, details: dict[str, Any], now: datetime
    ) -> None:
        async with self._db.scoped_transaction(Scope(user_id)) as conn:
            await conn.execute(
                "INSERT INTO deletion_requests (user_id, scope, requested_at, status, completed_at, details_json) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, scope, now, status, now if status == "completed" else None, Jsonb(details)),
            )

    # --- スケジューラ用(内容は返さない) ---

    async def _targets(self, sql: str) -> list[tuple[Any, ...]]:
        async with self._db.pool.connection() as conn:
            return await (await conn.execute(sql)).fetchall()

    async def consolidation_targets(self) -> list[Target]:
        rows = await self._targets("SELECT user_id, character_id, timezone, oldest_day FROM list_consolidation_targets()")
        return [Target(*row) for row in rows]

    async def all_scopes(self) -> list[Target]:
        rows = await self._targets("SELECT user_id, character_id, timezone FROM list_memory_scopes()")
        return [Target(*row) for row in rows]

    async def idle_sessions(self, cutoff: datetime) -> list[tuple[uuid.UUID, str, uuid.UUID]]:
        async with self._db.pool.connection() as conn:
            cursor: psycopg.AsyncCursor[Any] = await conn.execute(
                "SELECT user_id, character_id, session_id FROM list_idle_sessions(%s)", (cutoff,)
            )
            return [(row[0], row[1], row[2]) for row in await cursor.fetchall()]

    async def proactive_candidates(self, before: datetime) -> list[tuple[uuid.UUID, str, str]]:
        """自発発話の候補(許可したユーザーで、最後の会話が `before` より前の組。§15.1 の M8)。ID とタイムゾーンだけ。"""
        async with self._db.pool.connection() as conn:
            cursor: psycopg.AsyncCursor[Any] = await conn.execute(
                "SELECT user_id, character_id, timezone FROM list_proactive_candidates(%s)", (before,)
            )
            return [(row[0], row[1], row[2]) for row in await cursor.fetchall()]
