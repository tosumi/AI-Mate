"""感情状態の管理(本体仕様書 §4.2、§7.1)。

感情の状態は、Plutchik の8感情(各 0〜1)と VAD(各 -1〜1)のベクトルで持つ。
ユーザーの状態もキャラクタの状態も、同じ座標系で扱う。

- ユーザー: instant(毎ターンの推定)、session_mood(指数移動平均)、baseline。
- キャラクタ: ターンごとに、基準への回帰・ユーザー感情への同期・話題ルールで更新する。
  ユーザー感情を別の感情へ変換する「写像」は行わない(§7.1)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mycompanion.emotion.models import CATEGORY_NAMES, EmotionEstimate
from mycompanion.state.events import EventOutcome

VAD_NAMES = ("valence", "arousal", "dominance")

# ユーザーの session_mood の指数移動平均(初期値)
MOOD_ALPHA = 0.3
MOOD_MAX_STEP = 0.3  # 1ターンの変化の上限(急変を抑える)

# 話題ルール(§5.2)
RULE_FIRE_THRESHOLD = 0.5  # 関連度がこれ未満なら発火しない
RULE_REPEAT_FACTOR = 0.5  # 直前のターンでも発火したルールは、効果を弱める(毎回満額にしない)


@dataclass
class EmotionVector:
    categories: dict[str, float] = field(default_factory=lambda: dict.fromkeys(CATEGORY_NAMES, 0.0))
    vad: dict[str, float] = field(default_factory=lambda: dict.fromkeys(VAD_NAMES, 0.0))

    @classmethod
    def from_estimate(cls, estimate: EmotionEstimate) -> EmotionVector:
        return cls(categories=estimate.categories.model_dump(), vad=estimate.vad.model_dump())

    @classmethod
    def from_json(cls, categories: dict[str, Any] | None, vad: dict[str, Any] | None) -> EmotionVector:
        vector = cls()
        for name in CATEGORY_NAMES:
            vector.categories[name] = float((categories or {}).get(name, 0.0))
        for name in VAD_NAMES:
            vector.vad[name] = float((vad or {}).get(name, 0.0))
        return vector

    def copy(self) -> EmotionVector:
        return EmotionVector(dict(self.categories), dict(self.vad))

    def rounded(self) -> EmotionVector:
        return EmotionVector(
            {k: round(v, 4) for k, v in self.categories.items()}, {k: round(v, 4) for k, v in self.vad.items()}
        )

    def dominant(self) -> tuple[str, float]:
        name = max(CATEGORY_NAMES, key=lambda n: self.categories[n])
        return name, self.categories[name]


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# ユーザーの感情状態
# ---------------------------------------------------------------------------


def update_session_mood(mood: EmotionVector | None, instant: EmotionVector) -> EmotionVector:
    """session_mood を、指数移動平均で更新する。セッションの最初のターンは、推定値をそのまま使う。"""
    if mood is None:
        return instant.copy()
    result = mood.copy()
    for values, new_values, low in (
        (result.categories, instant.categories, 0.0),
        (result.vad, instant.vad, -1.0),
    ):
        for name, old in values.items():
            step = MOOD_ALPHA * (new_values[name] - old)
            step = _clip(step, -MOOD_MAX_STEP, MOOD_MAX_STEP)
            values[name] = _clip(old + step, low, 1.0)
    return result


def update_companion_mood(mood: EmotionVector | None, instant: EmotionVector) -> EmotionVector:
    """キャラクタの session_mood。instant 層 `c` の指数移動平均(ユーザーの session_mood と同じ式)。"""
    return update_session_mood(mood, instant)


def neutral_baseline() -> EmotionVector:
    """新しいキャラクタとの開始時の baseline(中立)。他のキャラクタの baseline からは初期化しない(§4.2)。"""
    return EmotionVector(categories=dict.fromkeys(CATEGORY_NAMES, 0.1))


# ---------------------------------------------------------------------------
# キャラクタの感情状態(§7.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Temperament:
    baseline: EmotionVector
    reactivity: float
    recovery_rate: float

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Temperament:
        data = data or {}
        vad = data.get("baseline_vad") or {}
        baseline = EmotionVector()
        # 感情カテゴリの基準値(回帰の先)。省略した感情は 0
        for name, value in (data.get("baseline_categories") or {}).items():
            if name in baseline.categories:
                baseline.categories[name] = _clip(float(value), 0.0, 1.0)
        baseline.vad = {
            "valence": float(vad.get("v", 0.0)),
            "arousal": float(vad.get("a", 0.0)),
            "dominance": float(vad.get("d", 0.0)),
        }
        return cls(
            baseline=baseline,
            reactivity=float(data.get("reactivity", 0.5)),
            recovery_rate=float(data.get("recovery_rate", 0.3)),
        )


@dataclass(frozen=True)
class RuleFiring:
    """発火した話題ルール(`emotion_events` と `last_triggers_json` に記録する。§5.2)。"""

    index: int
    topic: str
    relevance: float
    scale: float  # gain × relevance × reactivity ×(繰り返しの減衰)
    effects: dict[str, float]


def fire_rules(
    rules: list[dict[str, Any]],
    matches: dict[int, float],
    reactivity: float,
    previous_indexes: set[int] | None = None,
) -> list[RuleFiring]:
    """関連度が閾値以上の話題ルールを発火させる。"""
    previous_indexes = previous_indexes or set()
    firings = []
    for index, relevance in sorted(matches.items()):
        if not 0 <= index < len(rules) or relevance < RULE_FIRE_THRESHOLD:
            continue
        rule = rules[index]
        if rule.get("origin") == "sheet":
            continue  # 好き嫌いシート由来のルールは、条件つきで別に評価する(`state.appraisal`)
        scale = float(rule.get("gain", 0.0)) * relevance * reactivity
        if index in previous_indexes:
            scale *= RULE_REPEAT_FACTOR
        firings.append(
            RuleFiring(
                index=index,
                topic=str(rule.get("topic", "")),
                relevance=round(relevance, 4),
                scale=round(scale, 4),
                effects={k: float(v) for k, v in (rule.get("effects") or {}).items()},
            )
        )
    return firings


@dataclass
class CompanionUpdate:
    state: EmotionVector
    triggers: dict[str, Any]  # 各項の寄与(デバッグ用。`companion_state.last_triggers_json`)


def update_companion(
    previous: EmotionVector,
    temperament: Temperament,
    user_instant: EmotionVector | None,
    sync: float,
    firings: list[RuleFiring],
    events: EventOutcome | None = None,
    skip_sync: tuple[str, ...] = (),
    sheet: dict[str, float] | None = None,
) -> CompanionUpdate:
    """c(t) = clip( c(t-1) + ①回帰 + ②同期 + ③話題ルール + ④イベントルール )。

    `c` は、キャラクタの感情の instant 層(ターンをまたいで持ち越す内部状態)。すべての項を、`c(t-1)` から計算して足す。
    `sync` は w(r) × confidence。`user_instant` が None(推定の縮退時)なら、同期しない。
    `skip_sync` の感情カテゴリは、同期しない(ユーザーの怒りがキャラクタに向いているとき、「一緒に怒る」方向に
    同期させないため。反応は、イベントルールに任せる)。
    """
    state = previous.copy()
    recovery: dict[str, float] = {}
    synced: dict[str, float] = {}
    ruled: dict[str, float] = {}

    for values, base, target, low in (
        (state.categories, temperament.baseline.categories, user_instant.categories if user_instant else None, 0.0),
        (state.vad, temperament.baseline.vad, user_instant.vad if user_instant else None, -1.0),
    ):
        for name, old in list(values.items()):
            r = temperament.recovery_rate * (base[name] - old)
            s = sync * (target[name] - old) if target is not None and name not in skip_sync else 0.0
            values[name] = _clip(old + r + s, low, 1.0)
            if abs(r) > 1e-6:
                recovery[name] = round(r, 4)
            if abs(s) > 1e-6:
                synced[name] = round(s, 4)

    for firing in firings:
        for name, effect in firing.effects.items():
            if name not in state.categories:
                continue
            change = effect * firing.scale
            state.categories[name] = _clip(state.categories[name] + change, 0.0, 1.0)
            ruled[name] = round(ruled.get(name, 0.0) + change, 4)

    # ③' 好き嫌いシート由来のルール(ガード・文脈の条件・繰り返し・上限を適用済みの変化量)
    sheeted: dict[str, float] = {}
    for name, change in (sheet or {}).items():
        if name in state.categories:
            state.categories[name] = _clip(state.categories[name] + change, 0.0, 1.0)
            sheeted[name] = change

    # ④イベントルール(ガード・逓減・上限を適用済みの変化量)。話題ルールと同じく、足すだけ
    evented: dict[str, float] = {}
    if events is not None:
        for name, change in events.categories.items():
            state.categories[name] = _clip(state.categories[name] + change, 0.0, 1.0)
            evented[name] = change
        for name, change in events.vad.items():
            state.vad[name] = _clip(state.vad[name] + change, -1.0, 1.0)
            evented[name] = change

    triggers = {
        "sync_weight": round(sync, 4),
        "recovery": recovery,
        "sync": synced,
        "rules": [
            {"index": f.index, "topic": f.topic, "relevance": f.relevance, "scale": f.scale} for f in firings
        ],
        "rule_effects": ruled,
        "sheet_effects": sheeted,
        "event_effects": evented,
        "sync_skipped": list(skip_sync),
    }
    return CompanionUpdate(state=state.rounded(), triggers=triggers)
