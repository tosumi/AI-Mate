"""運営者の API の保存先(PostgreSQL・Cosmos DB。本体仕様書 §15.1 の M9)。

- 監査ログ: audit_logs に追記する。
- 報告・監査ログの閲覧: 0021 の運営者用の関数(SECURITY DEFINER)だけを使う。
- ガードレールの記録: 報告の対象のターンを、Cosmos DB から、そのユーザー・キャラクタの範囲で引く。
  ターンの記録には本文が含まれるので、返す項目は `ops.views` で選ぶ。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mycompanion.db.cosmos import CosmosStores
from mycompanion.db.postgres import Database
from mycompanion.ops.audit import OperatorAuditEntry


class PgOperatorAuditLog:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def record(self, entry: OperatorAuditEntry) -> None:
        # アプリのロールは、audit_logs に追記だけできる(読めない・変えられない)
        async with self._db.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO audit_logs (ts, actor_type, actor_id, action, target_user_id, details_json) "
                "VALUES (%s, 'operator', %s, %s, %s, %s)",
                (
                    entry.ts,
                    entry.actor_id,
                    entry.action,
                    entry.target_user_id,
                    Jsonb({"decision": entry.decision, "roles": list(entry.roles), **entry.details}),
                ),
            )


class PgOperatorStore:
    def __init__(self, database: Database, cosmos: CosmosStores) -> None:
        self._db = database
        self._cosmos = cosmos

    async def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._db.pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            names = [d.name for d in cursor.description or []]
            rows = await cursor.fetchall()
        return [dict(zip(names, row, strict=True)) for row in rows]

    async def list_reports(self, status: str | None, before: datetime | None, limit: int) -> list[dict[str, Any]]:
        return await self._rows("SELECT * FROM ops_list_reports(%s, %s, %s)", (status, before, limit))

    async def get_report(self, report_id: uuid.UUID) -> dict[str, Any] | None:
        rows = await self._rows("SELECT * FROM ops_get_report(%s)", (report_id,))
        return rows[0] if rows else None

    async def update_report(
        self, report_id: uuid.UUID, status: str | None, memo: str | None, operator_id: str, now: datetime
    ) -> bool:
        async with self._db.pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "SELECT ops_update_report(%s, %s, %s, %s, %s)", (report_id, status, memo, operator_id, now)
                )
            ).fetchone()
        return bool(row and row[0])

    async def turn_record(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, turn_no: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        turns = [
            item
            async for item in self._cosmos.turns.query(
                user_id, character_id, "c.sessionId = @sessionId AND c.turnNo = @turnNo",
                {"sessionId": str(session_id), "turnNo": turn_no}, limit=1,
            )
        ]  # fmt: skip
        if not turns:
            return None, []
        findings = [
            item
            async for item in self._cosmos.security_events.query(
                user_id, character_id, "c.turn_id = @turnId", {"turnId": turns[0]["id"]}, order_by="c.ts ASC"
            )
        ]
        return turns[0], findings

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        # 評価の集計の関数(0016 の rating_summary。件数と平均だけ。user_id・メモは返さない)
        return await self._rows("SELECT * FROM rating_summary(%s)", (since,))

    async def list_audit_logs(
        self,
        since: datetime | None,
        until: datetime | None,
        actor_type: str | None,
        before: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self._rows(
            "SELECT * FROM ops_list_audit_logs(%s, %s, %s, %s, %s)", (since, until, actor_type, before, limit)
        )


# ---------------------------------------------------------------------------
# キャラクタの提出・公開(0022 の関数だけを使う。§15.1 の M10)
# ---------------------------------------------------------------------------

_DB_ERRORS = {
    "InsufficientPrivilege": "forbidden",
    "CheckViolation": "conflict",
    "NoDataFound": "not_found",
}


class PgPersonaStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def _call(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        from mycompanion.ops.personas import PersonaError

        try:
            async with self._db.pool.connection() as conn, conn.transaction():
                cursor = await conn.execute(sql, params)
                names = [d.name for d in cursor.description or []]
                rows = await cursor.fetchall()
        except psycopg.errors.RaiseException as exc:  # 関数の中の RAISE(ERRCODE の指定なし)
            raise PersonaError("invalid", exc.diag.message_primary or "処理できませんでした") from None
        except psycopg.Error as exc:
            kind = _DB_ERRORS.get(type(exc).__name__)
            if kind is None:
                raise
            raise PersonaError(kind, exc.diag.message_primary or "処理できませんでした") from None
        return [dict(zip(names, row, strict=True)) for row in rows]

    async def submit(
        self, character_id: str, label: str, definition: dict[str, Any], row: dict[str, Any],
        lint_json: list[dict[str, Any]], simulation: str, operator: str, now: datetime,
    ) -> uuid.UUID:  # fmt: skip
        rows = await self._call(
            "SELECT ops_submit_persona(%s, %s, %s, %s, %s, %s, %s, %s) AS id",
            (character_id, label, Jsonb(definition), Jsonb(row), Jsonb(lint_json), simulation, operator, now),
        )
        return rows[0]["id"]

    async def list_submissions(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        return await self._call("SELECT * FROM ops_list_persona_submissions(%s, %s)", (status, limit))

    async def get_submission(self, submission_id: uuid.UUID) -> dict[str, Any] | None:
        rows = await self._call("SELECT * FROM ops_get_persona_submission(%s)", (submission_id,))
        return rows[0] if rows else None

    async def publish(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> int:
        rows = await self._call(
            "SELECT ops_publish_persona_submission(%s, %s, %s, %s) AS version", (submission_id, operator, notes, now)
        )
        return int(rows[0]["version"])

    async def reject(self, submission_id: uuid.UUID, operator: str, notes: str | None, now: datetime) -> bool:
        rows = await self._call(
            "SELECT ops_reject_persona_submission(%s, %s, %s, %s) AS ok", (submission_id, operator, notes, now)
        )
        return bool(rows[0]["ok"])

    async def list_versions(self, character_id: str | None) -> list[dict[str, Any]]:
        return await self._call("SELECT * FROM ops_list_persona_versions(%s)", (character_id,))

    async def get_version(self, character_id: str, version: int) -> dict[str, Any] | None:
        rows = await self._call("SELECT * FROM ops_get_persona_version(%s, %s)", (character_id, version))
        return rows[0] if rows else None

    async def rollback(self, character_id: str, label: str, operator: str, now: datetime) -> int:
        rows = await self._call(
            "SELECT ops_rollback_persona(%s, %s, %s, %s) AS version", (character_id, label, operator, now)
        )
        return int(rows[0]["version"])

    async def deprecate(self, character_id: str, operator: str, now: datetime) -> int:
        rows = await self._call("SELECT ops_deprecate_persona(%s, %s, %s) AS n", (character_id, operator, now))
        return int(rows[0]["n"])
