"""運営者の API に返す形(本体仕様書 §15.1 の M9 の手順2)。

運営者には、会話の本文を返さない。保存先から読んだ記録には本文が含まれうるので、ここで
**返してよい項目だけを選んで**組み立てる(除く項目を並べるのではなく、許す項目を並べる)。
ユーザーの ID は、元に戻せない仮の ID に置き換える。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from mycompanion.account.store import mask_small_groups

# 報告の対応状況
REPORT_STATUSES = ("open", "in_progress", "resolved")
# 対応メモの上限(0021 の CHECK と同じ)
OPS_MEMO_MAX = 500

# ガードレールの記録として返す、ターンの項目(応答の種類・安全の判定と、作り方の文脈。本文・下書き・計画は含めない)
GUARDRAIL_TURN_KEYS = (
    "response_mode", "filter_mode", "safety_flags", "silence_reason", "gate_rule_hits_json", "system_notices",
    "character_version", "compose_mode", "reply_model", "reply_prompt_version",
)  # fmt: skip
# スクリーニングの検出として返す項目(details は、検出の根拠を含みうるので返さない)
FINDING_KEYS = ("layer", "category", "severity", "detector", "score", "action")
# 監査ログの details_json のうち、返してよい項目
AUDIT_DETAIL_KEYS = (
    "decision", "roles", "permission", "target", "report_id", "status", "memo_changed",
    # キャラクタの提出・公開(§15.1 の M10)
    "character_id", "version_label", "submission_id", "outcome",
)  # fmt: skip


def pseudonym(user_id: uuid.UUID | str) -> str:
    """ユーザーの仮の ID(ユーザーの ID から作る、元に戻せない値。同じユーザーなら同じ値)。"""
    digest = hashlib.sha256(f"mycompanion-ops-user:{user_id}".encode()).hexdigest()
    return f"u-{digest[:16]}"


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def report_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": str(row["report_id"]),
        "user": pseudonym(row["user_id"]),
        "character_id": row["character_id"],
        "session_id": _str(row.get("session_id")),
        "turn_no": row.get("turn_no"),
        "reason": row["reason"],
        "has_note": bool(row.get("has_note")),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "ops_updated_at": _iso(row.get("ops_updated_at")),
    }


def guardrails(turn: dict[str, Any] | None, findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    if turn is None:
        return None
    view: dict[str, Any] = {key: turn.get(key) for key in GUARDRAIL_TURN_KEYS}
    view["ts"] = _iso(turn.get("ts"))
    view["screening"] = [{key: f.get(key) for key in FINDING_KEYS} for f in findings]
    return view


def report_detail(
    row: dict[str, Any], turn: dict[str, Any] | None, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        **report_summary({**row, "has_note": bool(row.get("note"))}),
        "note": row.get("note"),  # 本人が報告に書いたメモ(仕様書 §15.1 の M9。会話の本文ではない)
        "ops_memo": row.get("ops_memo"),
        "ops_updated_by": row.get("ops_updated_by"),
        "guardrails": guardrails(turn, findings),
    }


def audit_entry(row: dict[str, Any]) -> dict[str, Any]:
    actor_id = row.get("actor_id")
    if row.get("actor_type") == "user" and actor_id:
        actor_id = pseudonym(actor_id)  # ユーザーの操作は、仮の ID で示す
    details = row.get("details_json") or {}
    return {
        "log_id": str(row["log_id"]),
        "ts": _iso(row["ts"]),
        "actor_type": row["actor_type"],
        "actor_id": actor_id,
        "action": row["action"],
        "target_user": pseudonym(row["target_user_id"]) if row.get("target_user_id") else None,
        "details": {key: details[key] for key in AUDIT_DETAIL_KEYS if key in details},
    }


def rating_rows(rows: list[dict[str, Any]], min_n: int) -> list[dict[str, Any]]:
    """評価の集計。件数が少ない組は、平均を出さない(少数の評価から、個人の点が推測されないように)。"""
    # PostgreSQL の numeric(Decimal)は、JSON の数値にする
    return [
        {k: float(v) if isinstance(v, Decimal) else v for k, v in mask_small_groups(row, min_n).items()}
        for row in rows
    ]
