"""運営者の API が読み書きするデータの口(本体仕様書 §15.1 の M9 の手順2)。

保存先の記録には、会話の本文が含まれうる(ターンの記録)。返す形は `ops.views` で選ぶ。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from mycompanion.account.store import summarize_ratings


class OperatorStore(Protocol):
    async def list_reports(self, status: str | None, before: datetime | None, limit: int) -> list[dict[str, Any]]:
        """報告の一覧(新しい順)。各項目は 0021 の ops_list_reports の列。"""
        ...

    async def get_report(self, report_id: uuid.UUID) -> dict[str, Any] | None:
        """報告の詳細(0021 の ops_get_report の列。user_id を含む。応答には出さない)。"""
        ...

    async def update_report(
        self, report_id: uuid.UUID, status: str | None, memo: str | None, operator_id: str, now: datetime
    ) -> bool: ...

    async def turn_record(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, turn_no: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """報告の対象のターンの記録と、そのターンのスクリーニングの検出。"""
        ...

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]: ...

    async def list_audit_logs(
        self,
        since: datetime | None,
        until: datetime | None,
        actor_type: str | None,
        before: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class InMemoryOperatorStore:
    """テスト用。"""

    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []  # user_id / character_id / session_id / turn_no / turn_id と、記録の項目
        self.findings: list[dict[str, Any]] = []  # turn_id と、検出の項目
        self.ratings: list[dict[str, Any]] = []
        self.audit_logs: list[dict[str, Any]] = []

    async def list_reports(self, status: str | None, before: datetime | None, limit: int) -> list[dict[str, Any]]:
        rows = [
            r for r in self.reports
            if (status is None or r["status"] == status) and (before is None or r["created_at"] < before)
        ]  # fmt: skip
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [
            {**{k: r.get(k) for k in ("report_id", "user_id", "character_id", "session_id", "turn_no", "reason",
                                       "status", "created_at", "ops_updated_at")},
             "has_note": bool(r.get("note"))}
            for r in rows[: max(1, min(limit, 200))]
        ]  # fmt: skip

    async def get_report(self, report_id: uuid.UUID) -> dict[str, Any] | None:
        for r in self.reports:
            if r["report_id"] == report_id:
                return dict(r)
        return None

    async def update_report(
        self, report_id: uuid.UUID, status: str | None, memo: str | None, operator_id: str, now: datetime
    ) -> bool:
        for r in self.reports:
            if r["report_id"] == report_id:
                if status is not None:
                    r["status"] = status
                if memo is not None:
                    r["ops_memo"] = memo or None
                r["ops_updated_by"], r["ops_updated_at"] = operator_id, now
                return True
        return False

    async def turn_record(
        self, user_id: uuid.UUID, character_id: str, session_id: uuid.UUID, turn_no: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        for t in self.turns:
            if (t["user_id"], t["character_id"], t["session_id"], t["turn_no"]) == (
                user_id, character_id, session_id, turn_no
            ):  # fmt: skip
                return dict(t), [dict(f) for f in self.findings if f.get("turn_id") == t.get("turn_id")]
        return None, []

    async def rating_summary(self, since: datetime) -> list[dict[str, Any]]:
        return summarize_ratings([r for r in self.ratings if r["ts"] >= since])

    async def list_audit_logs(
        self,
        since: datetime | None,
        until: datetime | None,
        actor_type: str | None,
        before: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = [
            r for r in self.audit_logs
            if (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)
            and (actor_type is None or r["actor_type"] == actor_type) and (before is None or r["ts"] < before)
        ]  # fmt: skip
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return [dict(r) for r in rows[: max(1, min(limit, 500))]]
