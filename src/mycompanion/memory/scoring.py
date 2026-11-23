"""記憶の採点(本体仕様書 §9.3、§9.9.2、§9.10)。副作用のない計算だけを置く。

係数は初期値。評価で調整する(§16)。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from mycompanion.config import EngineSettings
from mycompanion.memory.models import Memory, RecallEvent

# 検索の重み(§9.3 の λ1〜λ4。λ5・λ6 は設定値)
L1_SIMILARITY = 0.5
L2_RECENCY = 0.15
L3_IMPORTANCE = 0.2
L4_EMOTION = 0.1
RECENCY_HALF_LIFE_DAYS = 30.0  # 中期の新しさの減衰
MIN_SIMILARITY = 0.25  # これ未満の記憶は、検索では選ばない(関係のない記憶を持ち出さない)
ACTIVATION_NORMALIZER = 3.0  # 活性度を 0〜1 に正規化するときの上限

# 層ごとの件数の上限(§9.3 の初期案)
BUDGET_SHORT = 5
BUDGET_MEDIUM = 5
BUDGET_LONG_FIXED = 3
BUDGET_LONG_SEARCH = 3

# 嗜好度の照合(§9.10.2)
PREFERENCE_MIN_RELEVANCE = 0.45  # 話題タグと嗜好の話題が、これ以上似ていれば関係ありとする
PREFERENCE_TOP_K = 3


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def emotion_closeness(memory: Memory, user_vad: dict[str, float] | None) -> float:
    """記憶の感情タグと、現在のユーザーの感情の近さ(0〜1)。どちらかが無ければ 0。"""
    if not memory.emotion_vad or not user_vad:
        return 0.0
    distance = math.sqrt(
        sum((float(memory.emotion_vad.get(k, 0.0)) - float(user_vad.get(k, 0.0))) ** 2 for k in ("valence", "arousal"))
    )
    return clip(1.0 - distance / 2.0, 0.0, 1.0)


def recency(memory: Memory, today: date) -> float:
    """新しさ(0〜1)。中期は経過日数で減衰する。長期は減衰させない(§9.3)。"""
    if memory.tier == "long":
        return 1.0
    day = memory.source_day or (memory.created_at.date() if memory.created_at else today)
    age = max(0, (today - day).days)
    return 2.0 ** (-age / RECENCY_HALF_LIFE_DAYS)


def retrieval_score(
    memory: Memory, similarity: float, today: date, user_vad: dict[str, float] | None, settings: EngineSettings
) -> float:
    """score = λ1×意味類似度 + λ2×新しさ + λ3×重要度 + λ4×感情の近さ + λ5×max(嗜好度, 0) + λ6×活性度。

    苦手な話題(嗜好度が負)は、減点しない(ユーザーが触れたら応答できるように。§9.10.3)。
    """
    return (
        L1_SIMILARITY * max(similarity, 0.0)
        + L2_RECENCY * recency(memory, today)
        + L3_IMPORTANCE * memory.importance
        + L4_EMOTION * emotion_closeness(memory, user_vad)
        + settings.memory_preference_weight * max(memory.preference, 0.0)
        + settings.memory_activation_weight * clip(memory.activation / ACTIVATION_NORMALIZER, 0.0, 1.0)
    )


def activation(events: list[RecallEvent], today: date, preference: float, settings: EngineSettings) -> float:
    """A_eff = Σ ω_j × 2^(−Δ_j / H) + γ × max(preference, 0)(§9.9.2)。

    想起は、日ごと・起点ごとに1回。同じ日に両方の起点があれば、大きいほうの重みを使う。
    確定していない(pending / discarded)キャラクタ起点の想起は、数えない。
    """
    per_day: dict[date, float] = {}
    for event in events:
        if event.status != "confirmed":
            continue
        per_day[event.day] = max(per_day.get(event.day, 0.0), event.weight)
    half_life = settings.recall_activation_half_life_days
    base = sum(weight * 2.0 ** (-max(0, (today - day).days) / half_life) for day, weight in per_day.items())
    return base + settings.activation_preference_gain * max(preference, 0.0)


def recall_day_counts(events: list[RecallEvent]) -> tuple[int, int]:
    """(想起日数, うちユーザー起点の日数)。確定した想起だけを数える(§9.9.5)。"""
    confirmed = [e for e in events if e.status == "confirmed"]
    return len({e.day for e in confirmed}), len({e.day for e in confirmed if e.trigger == "user"})


def preference_score(
    topic_vectors: list[list[float]], preferences: list[tuple[list[float], float]]
) -> float:
    """preference = clip(Σ_k relevance_k × affinity_k)。関連度の高い上位3件の嗜好を使う(§9.10.2)。

    `preferences` は(嗜好の話題の埋め込み, affinity)。
    """
    matches: list[tuple[float, float]] = []
    for vector, affinity in preferences:
        relevance = max((cosine(vector, topic) for topic in topic_vectors), default=0.0)
        if relevance >= PREFERENCE_MIN_RELEVANCE:
            matches.append((relevance, affinity))
    matches.sort(reverse=True)
    return clip(sum(r * a for r, a in matches[:PREFERENCE_TOP_K]), -1.0, 1.0)


def local_day(moment: datetime, timezone: str, consolidation_time: str = "04:00") -> date:
    """「その日」を返す。日の区切りは、ユーザーのローカル時刻の整理時刻(既定は午前4時。§9.4.2)。"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Asia/Tokyo")
    hours, minutes = (int(part) for part in consolidation_time.split(":"))
    return (moment.astimezone(zone) - timedelta(hours=hours, minutes=minutes)).date()


def average_vad(items: list[dict[str, Any]]) -> dict[str, float]:
    if not items:
        return {"valence": 0.0, "arousal": 0.0, "dominance": 0.0}
    return {
        key: round(sum(float(item.get(key, 0.0)) for item in items) / len(items), 4)
        for key in ("valence", "arousal", "dominance")
    }
