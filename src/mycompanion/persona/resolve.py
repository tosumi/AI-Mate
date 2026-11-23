"""表出パラメータの解決と、関係値による変調(本体仕様書 §6.3.3、§6.3.7、§7.2)。

設計時: 軸 → 既定値 → 差分 → 上書き、の順で「基準値(関係値0のときの値)」を作る。
実行時: 基準値と、ユーザーごとの関係値から、実効値を計算する。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from mycompanion.memory import association
from mycompanion.persona.schema import (
    ASSOCIATION_KEYS,
    DELTA_KEYS,
    AppraisalRule,
    PersonaDefinition,
    Preference,
)
from mycompanion.persona.sheet import sheet_preferences

RELATIONSHIP_MAX = 20.0

# 表出プロファイルの名前つきプリセット(§6.3.3)。差分と、付属の initial_relationship
PROFILE_PRESETS: dict[str, dict[str, float]] = {
    "kuchibeta": {  # 口下手
        "talkativeness": -0.05, "articulation": -0.50, "expressiveness": -0.15, "initiative": -0.05,
        "relationship_sensitivity": 0.20,
    },
    "kamoku": {  # 寡黙
        "talkativeness": -0.20, "articulation": 0.10, "expressiveness": -0.20, "initiative": -0.15,
        "relationship_sensitivity": -0.20,
    },
    "hitonatsukoi": {  # 人懐こい
        "talkativeness": 0.05, "articulation": 0.10, "expressiveness": 0.15, "initiative": 0.15,
        "relationship_sensitivity": -0.20,
    },
    "oshaberi": {  # おしゃべり
        "talkativeness": 0.30, "articulation": 0.15, "expressiveness": 0.20, "initiative": 0.35,
        "relationship_sensitivity": -0.30,
    },
}  # fmt: skip
PROFILE_NAMES = {"kuchibeta": "口下手", "kamoku": "寡黙", "hitonatsukoi": "人懐こい", "oshaberi": "おしゃべり"}
PROFILE_INITIAL_RELATIONSHIP = {"kuchibeta": 0.0, "kamoku": 0.0, "hitonatsukoi": 6.0, "oshaberi": 3.0}
# 推奨の気質: (reactivity, recovery_rate, (v, a, d))
PROFILE_TEMPERAMENT = {
    "kuchibeta": (0.8, 0.2, (0.0, -0.1, -0.3)),
    "kamoku": (0.3, 0.4, (0.1, -0.3, 0.2)),
    "hitonatsukoi": (0.7, 0.4, (0.5, 0.3, 0.2)),
    "oshaberi": (0.8, 0.5, (0.3, 0.5, 0.3)),
}

# 嗜好の導出で、好み・苦手として扱う感情(デザイナ仕様書 §5.4.1)
_LIKE = ("joy", "trust", "anticipation")
_DISLIKE = ("fear", "disgust", "anger", "sadness")


@dataclass(frozen=True)
class Traits:
    """解決済みの表出パラメータ(基準値。関係値0のときの値)。"""

    introversion: float
    talkativeness: float
    articulation: float
    expressiveness: float
    initiative: float
    relationship_sensitivity: float

    def as_dict(self) -> dict[str, float]:
        return {key: round(value, 4) for key, value in asdict(self).items()}


def clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def default_traits(i: float, e: float) -> Traits:
    """軸から既定値を計算する(抽象度 a と柔軟度 f は、数値には影響しない。§6.3.5)。"""
    return Traits(
        introversion=i,
        talkativeness=0.75 - 0.5 * i,
        articulation=0.7,
        expressiveness=0.35 + 0.25 * e + 0.15 * (1 - i),
        initiative=0.6 - 0.4 * i,
        relationship_sensitivity=0.5,
    )


def resolve_traits(
    i: float, e: float, deltas: dict[str, float] | None = None, overrides: dict[str, float] | None = None
) -> Traits:
    """既定値 + 差分(0〜1 にクリップ)、上書きがあれば最優先。"""
    values = asdict(default_traits(i, e))
    for key, delta in (deltas or {}).items():
        if key not in DELTA_KEYS:
            raise KeyError(f"差分を指定できないパラメータです: {key}")
        values[key] = clip01(values[key] + delta)
    for key, value in (overrides or {}).items():
        if key not in values:
            raise KeyError(f"上書きできないパラメータです: {key}")
        values[key] = value
    return Traits(**values)


def resolve(definition: PersonaDefinition) -> Traits:
    axes = definition.base_personality.axes
    deltas = {k: v for k, v in definition.profile.deltas.items() if k not in ASSOCIATION_KEYS}
    return resolve_traits(axes.i, axes.e, deltas, definition.overrides)


# 表出プロファイルのプリセットに付く、話題の移りやすさの差分(おしゃべりは移りやすく、寡黙は移りにくい)
PROFILE_TOPIC_SHIFT = {"oshaberi": 0.15, "kamoku": -0.15}


def resolve_association(definition: PersonaDefinition) -> dict[str, float]:
    """連想のしやすさ・話題の移りやすさ(軸からの既定値 + プリセットの差分 + 差分)。0〜1。"""
    axes = definition.base_personality.axes.model_dump()
    deltas = definition.profile.deltas
    preset = PROFILE_TOPIC_SHIFT.get(definition.profile.preset or "", 0.0)
    ease = association.default_ease(axes) + deltas.get("association_ease", 0.0)
    shift = association.default_shift(axes) + preset + deltas.get("topic_shift", 0.0)
    return {"association_ease": round(clip01(ease), 4), "topic_shift": round(clip01(shift), 4)}


# ---------------------------------------------------------------------------
# 実行時: 関係値による変調(§6.3.7)と、感情表出の閾値・上限(§7.2)
# ---------------------------------------------------------------------------


def modulation(relationship: float, sensitivity: float, modulation_max: float) -> float:
    """M(r) = relationship_modulation_max × relationship_sensitivity × (r / 20)。"""
    return modulation_max * sensitivity * (relationship / RELATIONSHIP_MAX)


@dataclass(frozen=True)
class EffectiveTraits:
    """そのターンの実効値。`turns` に記録する(§12)。"""

    talkativeness_eff: float  # 応答長を決める
    expressiveness_eff: float  # 感情表出の閾値と上限を決める
    talkativeness_for_silence: float  # 無言スコア用。変調の正の部分だけを反映する
    expression_threshold: float  # θ = 0.7 − 0.5e
    expression_cap: float  # cap = 0.3 + 0.7e


def effective_traits(traits: Traits, relationship: float, modulation_max: float) -> EffectiveTraits:
    m = modulation(relationship, traits.relationship_sensitivity, modulation_max)
    expressiveness_eff = clip01(traits.expressiveness + m)
    return EffectiveTraits(
        talkativeness_eff=clip01(traits.talkativeness + m),
        expressiveness_eff=expressiveness_eff,
        # 関係値が低いことを理由に、無言を増やさない(罰としての無言にしない。§13.2)
        talkativeness_for_silence=clip01(traits.talkativeness + max(m, 0.0)),
        expression_threshold=0.7 - 0.5 * expressiveness_eff,
        expression_cap=0.3 + 0.7 * expressiveness_eff,
    )


# ---------------------------------------------------------------------------
# 嗜好の導出(デザイナ仕様書 §5.4.1)
# ---------------------------------------------------------------------------


def derived_preferences(rules: list[AppraisalRule]) -> list[Preference]:
    """話題ルールから嗜好を導出する。好みの感情は正、苦手の感情は負として、gain を掛ける。"""
    result = []
    for rule in rules:
        like = sum(rule.effects.get(name, 0.0) for name in _LIKE)
        dislike = sum(rule.effects.get(name, 0.0) for name in _DISLIKE)
        affinity = max(-1.0, min(1.0, (like - dislike) * rule.gain))
        if affinity != 0:
            result.append(Preference(topic=rule.topic, affinity=round(affinity, 4)))
    return result


def merged_preferences(definition: PersonaDefinition) -> list[dict[str, object]]:
    """明示した嗜好、好き嫌いシートの嗜好、話題ルールから導出した嗜好を合わせる。

    同じ話題は、明示 > シート > 導出の順で優先する。
    """
    explicit = {p.topic: p for p in definition.preferences}
    merged: list[dict[str, object]] = [
        {"topic": p.topic, "affinity": p.affinity, "source": "explicit"} for p in explicit.values()
    ]
    seen = set(explicit)
    for item in sheet_preferences(definition.likes_dislikes):
        if item["topic"] not in seen:
            merged.append(item)
            seen.add(item["topic"])
    for p in derived_preferences(definition.appraisal_rules):
        if p.topic not in seen:
            merged.append({"topic": p.topic, "affinity": p.affinity, "source": "derived"})
    return merged
