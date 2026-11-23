"""記憶のデータ(本体仕様書 §9、§12)。

記憶は、短期(short)・中期(medium)・長期(long)の3層で持つ。層とは別に、内容の種類(kind)がある。
すべての記憶は `(user_id, character_id)` ごとに分離する(§13.4.6)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

TIERS = ("short", "medium", "long")
KINDS = ("episodic", "semantic", "relationship", "monthly_summary", "interaction_preference")
# 「大切なこと」の種類(時系列の整理とは別に、ずっと残す記憶)。user_name は1件だけで、言い直されたら置き換える
CORE_KEYS = ("user_name", "profile", "preference", "relation", "request", "other")
CORE_SINGLE = ("user_name",)
# センシティブ項目の区分(§9.7)。none 以外は、同意がない限り、中期に移さない。
# third_party(ユーザー以外の人の内容)は、同意があっても残さない
SENSITIVITIES = ("none", "health", "belief", "other", "third_party")


@dataclass
class Memory:
    memory_id: uuid.UUID
    tier: str
    kind: str
    summary: str
    topics: list[str] = field(default_factory=list)
    emotion_vad: dict[str, float] | None = None  # 当時の感情(valence / arousal / dominance)
    emotion_categories: dict[str, float] | None = None
    importance: float = 0.0
    sensitivity: str | None = None
    pinned: bool = False  # 「覚えておいて」と明示された
    core_key: str | None = None  # 「大切なこと」の種類(None なら、ふつうの記憶)
    muted: bool = False  # キャラクタが自分から持ち出さない
    quarantined: bool = False  # 隔離(検索対象外、昇格不可。§13.5.6)
    quarantine_reason: str | None = None
    trust_level: str = "normal"
    preference: float = 0.0  # 嗜好度(-1〜+1。§9.10)
    preference_version: int | None = None
    activation: float = 0.0  # 活性度のキャッシュ(§9.9.2)
    origin_memory_id: uuid.UUID | None = None  # 再活性化の参照コピーの原本
    last_mentioned_by_character_at: datetime | None = None
    last_reactivated_at: datetime | None = None  # 最後に再活性化した日時(クールダウンの判定)
    source_day: date | None = None
    period: str | None = None  # monthly_summary: 対象月(YYYY-MM)
    expires_at: datetime | None = None
    recall_days: int = 0
    user_recall_days: int = 0
    last_recalled_at: datetime | None = None
    promoted_from: uuid.UUID | None = None
    promoted_at: datetime | None = None
    consolidated_at: datetime | None = None
    source_event_ids: list[str] = field(default_factory=list)  # 根拠となる会話のターン
    source_memory_ids: list[str] = field(default_factory=list)
    stale: bool = False
    embedding: list[float] | None = None
    version: int = 1
    created_at: datetime | None = None
    deleted_at: datetime | None = None
    # 想起の対象から外す理由(topic_archived = アーカイブしたトピックにだけ属していた。§9.13)。
    # 期限・昇格の判定は、今までどおり
    recall_excluded: str | None = None

    @property
    def is_copy(self) -> bool:
        """再活性化の参照コピーか(§9.9.4)。"""
        return self.origin_memory_id is not None

    @property
    def searchable(self) -> bool:
        return self.deleted_at is None and not self.quarantined and self.recall_excluded is None

    def view(self, debug: bool = False) -> dict[str, Any]:
        """利用者に見せる形(`/memory list`、`/memory show`)。埋め込みは含めない。"""
        data: dict[str, Any] = {
            "id": str(self.memory_id),
            "tier": self.tier,
            "kind": self.kind,
            "summary": self.summary,
            "topics": self.topics,
            "importance": round(self.importance, 2),
            "pinned": self.pinned,
            "core": self.core_key,
            "muted": self.muted,
            "source_day": self.source_day.isoformat() if self.source_day else None,
            "period": self.period,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if debug:
            data |= {
                "activation": round(self.activation, 3),
                "preference": round(self.preference, 3),
                "recall_days": self.recall_days,
                "user_recall_days": self.user_recall_days,
                "last_recalled_at": self.last_recalled_at.isoformat() if self.last_recalled_at else None,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "quarantined": self.quarantined,
                "sensitivity": self.sensitivity,
                "origin_memory_id": str(self.origin_memory_id) if self.origin_memory_id else None,
                "promoted_from": str(self.promoted_from) if self.promoted_from else None,
                "stale": self.stale,
            }
        return data


@dataclass
class RecallEvent:
    """想起の記録(§9.9.1)。記憶ごと・日ごと・起点ごとに1回。"""

    memory_id: uuid.UUID
    day: date
    trigger: str  # user / character
    source: str  # search / topic_selection / working_set
    weight: float
    status: str = "confirmed"  # character 起点: pending → confirmed / discarded
    ts: datetime | None = None
    session_id: uuid.UUID | None = None
    turn_id: str | None = None


@dataclass
class WorkingItem:
    """セッション内ワーキングセットの1件(§9.9.3)。"""

    memory_id: uuid.UUID
    last_recalled_turn: int


@dataclass
class Retrieved:
    """検索で選ばれ、プロンプトに入れる記憶。"""

    memory: Memory
    slot: str  # short / medium / long_fixed / long_summary / long_search / working_set / topic
    score: float = 0.0
    similarity: float = 0.0


@dataclass
class RunRecord:
    """整理の実行の記録(`memory_consolidation_runs`)。"""

    run_type: str  # daily / monthly / promotion
    target: str  # 対象日(YYYY-MM-DD)または対象月(YYYY-MM)
    status: str  # success / failed / skipped
    counts: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    error: str | None = None
