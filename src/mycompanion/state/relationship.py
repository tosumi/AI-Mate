"""関係値(本体仕様書 §6.2、§7.1)。

キャラクタから見た、特定のユーザーへの関係の度合い(-20〜+20)。
会話の積み重ねで、ゆっくり変化させる。1ターンで大きく動かさない。

更新の要因と変化量は初期案で、実データをもとに調整する(§16)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import EmotionEstimate

R_MIN, R_MAX = -20.0, 20.0

# 変化量の係数(初期案)
GAIN_POSITIVE = 0.3  # ユーザーの好意・感謝(キャラクタに向いたもの)
GAIN_NEGATIVE = 0.5  # キャラクタに向けた怒り・侮辱・拒絶
DELTA_NEW_DAY = 0.1  # 日をまたいだ交流の継続
EMOTION_THRESHOLD = 0.4  # これ未満の感情では、関係値を動かさない


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    description: str  # 下書きに渡す、関係性の記述(数値は渡さない。§8.1)


_BANDS = [
    (-11.0, Band("hostile", "敵対・嫌悪", "この相手には強い嫌悪があり、関わりたくないと感じている。素っ気なく、必要なことだけを短く返す。")),
    (-4.0, Band("cold", "冷淡・警戒", "この相手を警戒していて、距離を置いている。打ち解けた話し方はせず、控えめに返す。")),
    (3.0, Band("neutral", "中立", "この相手とは、まだ特別な関係がない。初対面に近い、ふつうの距離感で話す。")),
    (10.0, Band("friendly", "友好", "この相手には親しみを持っている。少しくだけた口調で、気軽に話す。")),
    (20.0, Band("affection", "親愛", "この相手をとても大切に思っている。心を開いて、親しく話す。")),
]  # fmt: skip


def clip(value: float) -> float:
    return max(R_MIN, min(R_MAX, value))


def band(relationship: float) -> Band:
    """関係値の帯(§6.2)。"""
    r = round(relationship)
    for upper, item in _BANDS:
        if r <= upper:
            return item
    return _BANDS[-1][1]


def sync_weight(relationship: float, settings: EngineSettings) -> float:
    """同期の強さ w(r) = sync_max × (max(r, 0) / 20) ^ p。r ≤ 0 では同期しない(§7.1)。"""
    positive = max(relationship, 0.0) / R_MAX
    return settings.sync_max * positive**settings.sync_curve_p


@dataclass
class RelationshipChange:
    """1ターンの関係値の変化と、その根拠(`relationship_events` に記録する)。"""

    value_before: float
    value_after: float
    delta: float
    reasons: list[tuple[str, float]] = field(default_factory=list)  # (理由コード, 上限適用前の変化量)


def update(
    relationship: float,
    estimate: EmotionEstimate,
    settings: EngineSettings,
    *,
    session_delta: float = 0.0,
    new_day: bool = False,
    degraded: bool = False,
) -> RelationshipChange:
    """関係値を更新する。

    - `target` が `character` 以外の感情(別の出来事への怒りなど)では、下げない。
    - 1ターンの変化量は ±`relationship_delta_max_per_turn`、
      1セッションの変化量は ±`relationship_delta_max_per_session` に収める。
    - 推定が縮退動作のとき(`degraded`)は、感情による変化を行わない。
    """
    reasons: list[tuple[str, float]] = []
    categories = estimate.categories
    if not degraded and estimate.target == "character":
        positive = (categories.joy + categories.trust) / 2
        negative = max(categories.anger, categories.disgust)
        if negative >= EMOTION_THRESHOLD and negative >= positive:
            reasons.append(("anger_at_character", -GAIN_NEGATIVE * negative * estimate.confidence))
        elif positive >= EMOTION_THRESHOLD and estimate.vad.valence > 0:
            reasons.append(("affection_to_character", GAIN_POSITIVE * positive * estimate.confidence))
    if new_day:
        reasons.append(("continued_interaction", DELTA_NEW_DAY))

    delta = sum(amount for _, amount in reasons)
    # 1ターンの上限
    per_turn = settings.relationship_delta_max_per_turn
    delta = max(-per_turn, min(per_turn, delta))
    # 1セッションの上限(これまでの合計と合わせて、上限を超えない)
    per_session = settings.relationship_delta_max_per_session
    delta = max(-per_session - session_delta, min(per_session - session_delta, delta))
    after = clip(relationship + delta)
    return RelationshipChange(
        value_before=relationship,
        value_after=after,
        delta=round(after - relationship, 6),
        reasons=reasons,
    )
