"""ジョブの型と、メッセージの形(本体仕様書 §15.1 の「ジョブのキュー」)。

メッセージには、ジョブの種類と ID だけを入れる。会話・記憶の本文、メールアドレスなどは入れない。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, get_args

JobKind = Literal[
    "close_session",  # 無入力のセッションの終了と、短期記憶の書き込み
    "consolidate_daily",  # 1組(ユーザー × キャラクタ)の、未整理の日を古い順に
    "consolidate_monthly",  # 1組の前月分の概要と、期限切れの中期記憶の削除
    "delete_identity",  # 外部テナント(Entra External ID)のアカウントの削除
    "purge_audit_logs",  # 保持期間を過ぎた監査ログの削除
    "ping",  # 何もしない(キューの疎通の確認)
    "proactive",  # 1組(ユーザー × キャラクタ)の自発発話の判定と、話しかけ(§15.1 の M8)
]
JOB_KINDS: frozenset[str] = frozenset(get_args(JobKind))
SYSTEM_SESSION = "system"  # ユーザーに属さないジョブのセッション ID


class JobFormatError(ValueError):
    """メッセージの形が不正。再試行しても直らないので、デッドレターに送る。"""


@dataclass(frozen=True)
class Job:
    kind: str
    user_id: uuid.UUID | None = None
    character_id: str | None = None
    session_id: uuid.UUID | None = None  # close_session: 会話のセッション
    timezone: str | None = None  # 日次・月次: ユーザーのタイムゾーン(整理の日付の区切り)
    day: str | None = None  # consolidate_daily: 未整理の最も古い日(YYYY-MM-DD)
    period: str | None = None  # consolidate_monthly: YYYY-MM
    subject: str | None = None  # delete_identity: 外部テナントのユーザーのオブジェクト ID

    def __post_init__(self) -> None:
        if self.kind not in JOB_KINDS:
            raise JobFormatError(f"ジョブの種類が不正です: {self.kind}")
        needs_scope = self.kind in ("close_session", "consolidate_daily", "consolidate_monthly", "proactive")
        if needs_scope and (self.user_id is None or not self.character_id):
            raise JobFormatError(f"{self.kind} には user_id と character_id が必要です")
        if self.kind == "close_session" and self.session_id is None:
            raise JobFormatError("close_session には session_id が必要です")
        if self.kind == "consolidate_daily" and not self.day:
            raise JobFormatError("consolidate_daily には day が必要です")
        if self.kind == "consolidate_monthly" and not self.period:
            raise JobFormatError("consolidate_monthly には period が必要です")
        if self.kind == "delete_identity" and not self.subject:
            raise JobFormatError("delete_identity には subject が必要です")

    @property
    def queue_session(self) -> str:
        """キューのセッション ID。ユーザーごとに順に処理する(§11.5.4)。"""
        return str(self.user_id) if self.user_id is not None else SYSTEM_SESSION

    @property
    def label(self) -> str:
        """ログ用の短い表示(ID は含めない)。"""
        return self.kind

    def to_json(self) -> str:
        body: dict[str, Any] = {"v": 1, "kind": self.kind}
        for name in ("user_id", "session_id"):
            value = getattr(self, name)
            if value is not None:
                body[name] = str(value)
        for name in ("character_id", "timezone", "day", "period", "subject"):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        return json.dumps(body, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | bytes) -> Job:
        try:
            body = json.loads(text)
            if not isinstance(body, dict) or body.get("v") != 1:
                raise JobFormatError("メッセージの版が不正です")
            return cls(
                kind=str(body["kind"]),
                user_id=uuid.UUID(body["user_id"]) if body.get("user_id") else None,
                character_id=body.get("character_id"),
                session_id=uuid.UUID(body["session_id"]) if body.get("session_id") else None,
                timezone=body.get("timezone"),
                day=body.get("day"),
                period=body.get("period"),
                subject=body.get("subject"),
            )
        except JobFormatError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise JobFormatError("メッセージを読めません") from exc
