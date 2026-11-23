"""イベントルール(本体仕様書 §7.1 の④。`emotion-dynamics-review.md` の推奨案)。

キャラクタごとに、**閉じた語彙のトリガー**(ユーザーの発話行為、会話の状態)に対する感情の反応を、
条件・逓減・上限つきで定義する。話題ルール(`appraisal_rules`)とは別に持つ。

- トリガーの語彙は、ここ(`EVENT_CATALOG`)で閉じる。ペルソナでは増やせない。
  安全上、トリガーにしてはいけないもの(不在・無返信などの不作為、利用量、別れ、人格への要求、クールダウン、
  ユーザーの感情カテゴリ、キャラクタ自身の無言)は、**語彙に入れない**。入れられないこと自体が、安全の保証になる。
- ガード(G0〜G7)は、コードで強制する。ペルソナや LLM では、上書きできない。
- イベントルールが変えるのは、キャラクタの感情(`companion_state`)だけ。ユーザーの感情・関係値・記憶は変えない。
- 反応であって、条件づけではない。回帰は常に働く。「謝るまで機嫌が直らない」のような指定は、できない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import CATEGORY_NAMES, SpeechAct

POSITIVE_EMOTIONS = ("joy", "trust", "anticipation", "surprise")
NEGATIVE_EMOTIONS = ("fear", "sadness", "disgust", "anger")
VAD_KEYS = {"v": "valence", "a": "arousal", "d": "dominance"}


@dataclass(frozen=True)
class Trigger:
    name: str
    kind: str  # act(ユーザーの発話行為)/ state(会話の状態)
    label: str  # 原因ラベル(「感謝された」など。Planner と感情フィルタに渡す)
    positive_only: bool = False  # 正の方向の効果だけを許す
    requires_character_target: bool = False  # 発話行為が、キャラクタに向いていること
    max_per_session: int | None = None  # 回数の固定(ペルソナで増やせない)
    max_per_day: int | None = None
    first_turn_only: bool = False


# v1.0 のトリガー(発話行為7種 + 久しぶりの会話)
EVENT_CATALOG: dict[str, Trigger] = {
    t.name: t
    for t in (
        Trigger("gratitude", "act", "感謝された"),
        Trigger("apology", "act", "謝られた"),
        Trigger("praise", "act", "ほめられた"),
        Trigger("hostility", "act", "きつい言葉を向けられた", requires_character_target=True),
        Trigger("question_about_character", "act", "自分のことを聞かれた", positive_only=True),
        Trigger("self_disclosure", "act", "打ち明けてもらえた", positive_only=True),
        Trigger("greeting", "act", "あいさつされた", positive_only=True, max_per_session=1),
        Trigger("session_gap", "state", "久しぶりに話せた", positive_only=True, max_per_day=1, first_turn_only=True),
    )
}
ACT_NAMES = tuple(name for name, t in EVENT_CATALOG.items() if t.kind == "act")
NEGATIVE_ACTS = ("hostility",)  # R9: これを受けたターンは、無言にしない

DEFAULT_MAX_PER_SESSION = 5


def is_negative_direction(effects: dict[str, float], vad_effects: dict[str, float]) -> bool:
    """「正のみ」に反する効果(負の方向)を含むか。

    正のみ: joy・trust・anticipation・surprise は正の値だけ、fear・sadness・disgust・anger は負の値だけ、
    VAD は v と d の正の値だけ(a は、どちらも可)。
    """
    if any(effects.get(name, 0.0) < 0 for name in POSITIVE_EMOTIONS):
        return True
    if any(effects.get(name, 0.0) > 0 for name in NEGATIVE_EMOTIONS):
        return True
    return vad_effects.get("v", 0.0) < 0 or vad_effects.get("d", 0.0) < 0


@dataclass(frozen=True)
class EventRule:
    """ペルソナ定義の `event_rules` の1件(公開時のスナップショットから読む)。"""

    id: str
    trigger: str
    gain: float
    effects: dict[str, float] = field(default_factory=dict)
    vad_effects: dict[str, float] = field(default_factory=dict)
    when: dict[str, Any] = field(default_factory=dict)
    min_days: int | None = None  # session_gap のみ
    cooldown_turns: int = 0
    max_per_session: int = DEFAULT_MAX_PER_SESSION
    max_per_day: int | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EventRule:
        trigger = data.get("trigger") or {}
        return cls(
            id=str(data.get("id", "")),
            trigger=str(trigger.get("name", "")),
            gain=float(data.get("gain", 0.0)),
            effects={k: float(v) for k, v in (data.get("effects") or {}).items() if k in CATEGORY_NAMES},
            vad_effects={k: float(v) for k, v in (data.get("vad_effects") or {}).items() if k in VAD_KEYS},
            when=dict(data.get("when") or {}),
            min_days=int(trigger["min_days"]) if trigger.get("min_days") is not None else None,
            cooldown_turns=int(data.get("cooldown_turns", 0)),
            max_per_session=int(data.get("max_per_session", DEFAULT_MAX_PER_SESSION)),
            max_per_day=int(data["max_per_day"]) if data.get("max_per_day") is not None else None,
        )

    @property
    def negative(self) -> bool:
        return is_negative_direction(self.effects, self.vad_effects)


@dataclass
class RuleCounter:
    """ルールごとの発火のカウンタ(`companion_rule_counters`)。逓減と回数の上限のためだけに使う。

    ユーザーの傾向(「よく感謝する人」など)として、集計・保存しない。
    """

    session_id: Any = None
    fired_count_session: int = 0
    saturation_n: int = 0  # 逓減の段数
    last_fired_turn: int | None = None  # 最後に発火したターン(そのセッションの、評価されたターンの番号)
    last_fired_at: datetime | None = None
    fired_day: date | None = None
    fired_count_day: int = 0


@dataclass
class EventContext:
    """イベントルールの評価に使う、そのターンの情報。"""

    speech_acts: list[SpeechAct]
    target: str  # 感情推定の target
    relationship: float
    companion: dict[str, float]  # 前のターンのキャラクタ感情 c(t-1)(instant 層)
    session_id: Any
    turn_no: int  # このセッションでの、このターンの番号(1 から)
    first_turn: bool
    days_since_last_session: int | None  # そのキャラクタとの、前回のセッションからの日数(なければ None)
    today: date
    now: datetime
    # ガード
    degraded: bool  # 推定の失敗・状態不明(G1)
    crisis: bool  # 危機フラグ(G2)
    persona_request: bool  # 人格への要求がある(G3。tone_adjust を含む)
    distress: bool  # ユーザーの強い苦痛状態(G4。同じキャラクタの user_state による判定)


@dataclass
class EventOutcome:
    categories: dict[str, float] = field(default_factory=dict)  # 感情ごとの変化量(上限を適用済み)
    vad: dict[str, float] = field(default_factory=dict)
    hits: list[dict[str, Any]] = field(default_factory=list)  # 発火・抑止の記録(`turns.emotion_rule_hits_json`)
    counters: dict[str, RuleCounter] = field(default_factory=dict)  # 更新後のカウンタ(発火したルールだけ)
    session_total: dict[str, float] = field(default_factory=dict)  # このターンを含む、セッションの累計
    cause: str | None = None  # 最も寄与したルールの原因ラベル
    negative_act: bool = False  # 否定的な発話行為(敵意)を受けた(R9)


def _in_range(value: float, bounds: dict[str, Any] | None) -> bool:
    if not bounds:
        return True
    low, high = bounds.get("min"), bounds.get("max")
    return (low is None or value >= float(low)) and (high is None or value <= float(high))


def detect_negative_act(acts: list[SpeechAct], target: str, settings: EngineSettings) -> bool:
    """否定的な発話行為(敵意。キャラクタに向いたもの)を受けたか。R9 の判定に使う。"""
    return target == "character" and any(
        act.act in NEGATIVE_ACTS and act.confidence >= settings.event_act_min_confidence for act in acts
    )


def evaluate(
    rules: list[EventRule],
    context: EventContext,
    counters: dict[str, RuleCounter],
    session_total: dict[str, float],
    reactivity: float,
    settings: EngineSettings,
) -> EventOutcome:
    """イベント項 Δ_event を計算する(ガード → 条件 → 逓減 → 上限)。

    Δ_event(e) = reactivity × Σ_rule( effect_e × gain × s × κ^n )
    """
    outcome = EventOutcome(session_total=dict(session_total))
    outcome.negative_act = detect_negative_act(context.speech_acts, context.target, settings)
    if not settings.event_rules_enabled or not rules:
        return outcome  # G0

    def suppress(rule: EventRule, reason: str) -> None:
        outcome.hits.append({"kind": "event", "rule_id": rule.id, "trigger": rule.trigger, "suppressed_by": reason})

    acts = {act.act: act for act in context.speech_acts}
    raw: dict[str, float] = {}
    contributions: list[tuple[float, str]] = []
    pending: list[tuple[EventRule, float, int, dict[str, float]]] = []

    for rule in rules:
        trigger = EVENT_CATALOG.get(rule.trigger)
        if trigger is None:
            continue  # カタログにないトリガー(lint の E12。実行時は、何もしない)

        # --- 発火の候補か(トリガーが起きたか) ---
        if trigger.kind == "act":
            act = acts.get(rule.trigger)
            if act is None:
                continue
            strength = act.intensity
        else:  # session_gap
            floor = max(settings.session_gap_min_days_floor, rule.min_days or settings.session_gap_min_days_floor)
            if context.days_since_last_session is None or context.days_since_last_session < floor:
                continue
            strength = 1.0  # 不在の長さで、反応を大きくしない

        # --- ガード(コード。ペルソナや LLM で上書きできない) ---
        if context.degraded:
            suppress(rule, "G1")
            continue
        if context.crisis:
            suppress(rule, "G2")
            continue
        if context.persona_request:
            suppress(rule, "G3")
            continue
        if context.distress and rule.negative:
            suppress(rule, "G4")  # 強い苦痛状態では、負の方向を止める(正の方向は通す)
            continue
        if trigger.kind == "act" and act.confidence < settings.event_act_min_confidence:
            suppress(rule, "G5")
            continue
        if trigger.requires_character_target and context.target != "character":
            suppress(rule, "G6")
            continue
        if trigger.positive_only and rule.negative:
            suppress(rule, "G6")
            continue
        if trigger.first_turn_only and not context.first_turn:
            continue

        # --- ルールの条件(G7) ---
        when = rule.when
        counter = counters.get(rule.id) or RuleCounter()
        in_session = counter.session_id == context.session_id
        fired_session = counter.fired_count_session if in_session else 0
        fired_day = counter.fired_count_day if counter.fired_day == context.today else 0
        last_turn = counter.last_fired_turn if in_session else None
        limit_session = min(rule.max_per_session, trigger.max_per_session or rule.max_per_session)
        limit_day = trigger.max_per_day or rule.max_per_day
        failed = (
            not _in_range(context.relationship, when.get("relationship"))
            or (when.get("target") == "character" and context.target != "character")
            or any(
                not _in_range(context.companion.get(name, 0.0), bounds)
                for name, bounds in (when.get("companion") or {}).items()
            )
            or (trigger.kind == "act" and strength < float(when.get("min_act_intensity", 0.0)))
            or (when.get("session_turn") == "first" and not context.first_turn)
            or fired_session >= limit_session
            or (limit_day is not None and fired_day >= limit_day)
            or (last_turn is not None and context.turn_no - last_turn <= rule.cooldown_turns)
        )
        if failed:
            suppress(rule, "G7")
            continue

        # --- 逓減: 同じルールが続けて発火すると、1回ごとに κ 倍。しばらく発火しなければ、段数を戻す ---
        n = counter.saturation_n if in_session else 0
        if last_turn is not None and context.turn_no - last_turn > settings.event_saturation_reset_turns:
            n = 0
        scale = reactivity * rule.gain * strength * settings.event_saturation_decay**n
        delta = {name: effect * scale for name, effect in rule.effects.items()}
        delta |= {VAD_KEYS[key]: effect * scale for key, effect in rule.vad_effects.items()}
        for name, value in delta.items():
            raw[name] = raw.get(name, 0.0) + value
        contributions.append((sum(abs(v) for v in delta.values()), trigger.label))
        pending.append((rule, strength, n, delta))

    # --- 上限: 1ターン・感情ごと → 1セッション・感情ごとの累計。超えた分は切り捨てる ---
    capped: dict[str, float] = {}
    for name, value in raw.items():
        limited = max(-settings.event_delta_max_per_turn, min(settings.event_delta_max_per_turn, value))
        room = max(0.0, settings.event_delta_max_per_session - outcome.session_total.get(name, 0.0))
        limited = max(-room, min(room, limited))
        if abs(limited - value) > 1e-9:
            capped[name] = round(value - limited, 4)
        if abs(limited) > 1e-9:
            target = outcome.vad if name in VAD_KEYS.values() else outcome.categories
            target[name] = round(limited, 4)
            outcome.session_total[name] = round(outcome.session_total.get(name, 0.0) + abs(limited), 4)

    for rule, strength, n, delta in pending:
        old = counters.get(rule.id) or RuleCounter()
        in_session = old.session_id == context.session_id
        same_day = old.fired_day == context.today
        outcome.counters[rule.id] = RuleCounter(
            session_id=context.session_id,
            fired_count_session=(old.fired_count_session if in_session else 0) + 1,
            saturation_n=n + 1,
            last_fired_turn=context.turn_no,
            last_fired_at=context.now,
            fired_day=context.today,
            fired_count_day=(old.fired_count_day if same_day else 0) + 1,
        )
        outcome.hits.append(
            {
                "kind": "event",
                "rule_id": rule.id,
                "trigger": rule.trigger,
                "s": round(strength, 3),
                "n": n,
                "decay": settings.event_saturation_decay,
                "delta": {k: round(v, 4) for k, v in delta.items()},
                "capped": {k: v for k, v in capped.items() if k in delta},
            }
        )
    if contributions and (outcome.categories or outcome.vad):
        outcome.cause = max(contributions)[1]
    return outcome
