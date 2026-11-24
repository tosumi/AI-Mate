"""運営者の操作の監査ログ(本体仕様書 §15.1 の M9)。

- 運営者の API の要求は、許可・拒否のすべてを、操作を行う**前に**記録する。記録できなければ、操作を行わない。
- 会話の本文・報告のメモの本文は、記録しない(操作の名前、対象の ID、判定だけ)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# 判定
ALLOWED = "allowed"
DENIED = "denied"  # ロールが足りない
UNAUTHENTICATED = "unauthenticated"  # トークンがない・検証できない


@dataclass(frozen=True)
class OperatorAuditEntry:
    ts: datetime
    actor_id: str | None  # 運営者のオブジェクト ID(認証できなかったときは None)
    action: str  # 例: "GET /ops/v1/me"
    decision: str
    roles: tuple[str, ...] = ()
    target_user_id: uuid.UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


class OperatorAuditLog(Protocol):
    async def record(self, entry: OperatorAuditEntry) -> None:
        """1件記録する。失敗したら例外を投げる(呼び出し側は、操作を行わない)。"""
        ...


class InMemoryOperatorAuditLog:
    """テスト用。"""

    def __init__(self) -> None:
        self.entries: list[OperatorAuditEntry] = []
        self.fail = False

    async def record(self, entry: OperatorAuditEntry) -> None:
        if self.fail:
            raise RuntimeError("監査ログに書けません")
        self.entries.append(entry)
