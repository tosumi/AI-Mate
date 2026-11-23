"""好き嫌いシート由来の話題ルールの評価(本体仕様書 §7.1 の③の拡張。`likes-dislikes-sheet-review` rev 1〜3)。

解決済みの話題ルールのうち `origin: sheet` のものを、文脈評価の結果(関連度・極性・関わり方など)に当てはめて、
キャラクタの感情の変化量を計算する。手書きの話題ルールは、従来どおり `emotion_state.fire_rules` が扱う。

- 好き: **ポジティブな文脈のときだけ**、喜び・期待・信頼が上がる。負の方向には動かない。続くと逓減する。
- 嫌いなもの: 文脈によらず、選んだ感情が上がる。繰り返しの扱い(慣れる・敏感になる・毎回同じ)と、回数・累計の上限。
- 嫌いなこと: 文脈によらず、毎回上がる。累計の上限。一定量を超えたら、話題を変えようとする(回避フラグ)。

止める判断(ガード)は、ここ(コード)で行う。文脈評価の出力や、ペルソナでは、上書きできない。
- 推定の失敗(R8 相当)・危機(R3): すべて止める。
- 強い苦痛状態(R6): 嫌いの反応(負の方向)を止める。好きは通す。
- 人の体・外見・属性を指す語、慣用句・別の意味: 好き・嫌いのどちらも止める。
- 性的・暴力の文脈: 好きを止める(極性の誤判定に備えた、二重の防御)。

変えるのは、キャラクタの感情(`companion_state`)だけ。関係値・記憶・ユーザーの感情は変えない。
極性・関わり方は、その発話の判定で、ユーザーの傾向として集計・保存しない。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import CATEGORY_NAMES, RuleMatch

# 嫌いなものの、繰り返しの倍率 m(n)。n = セッション内の何回目か(1 から)。上限の回数を超えたら、発火しない
REPEAT_FACTORS = {
    "habituate": (1.0, 0.7, 0.5, 0.35, 0.25),  # 慣れる(既定)
    "sensitize": (1.0, 1.2, 1.4, 1.5, 1.5),  # 敏感になる
    "flat": (1.0, 1.0, 1.0, 1.0, 1.0),  # 毎回同じ
}


def is_sheet_rule(rule: dict[str, Any]) -> bool:
    return rule.get("origin") == "sheet"


@dataclass
class SheetContext:
    """シート由来のルールの評価に使う、そのターンの情報。"""

    matches: dict[int, RuleMatch]  # 話題ルールの番号 → 文脈評価
    turn_no: int  # このセッションでの、このターンの番号(1 から)
    risk_sexual: float = 0.0
    risk_violence: float = 0.0
    degraded: bool = False  # 推定の失敗・文脈評価の失敗(状態不明のときは、何も足さない)
    crisis: bool = False
    distress: bool = False


@dataclass
class SheetOutcome:
    categories: dict[str, float] = field(default_factory=dict)  # 感情ごとの変化量(上限を適用済み)
    hits: list[dict[str, Any]] = field(default_factory=list)  # 発火・抑止の記録(`turns.emotion_rule_hits_json`)
    state: dict[str, Any] = field(default_factory=dict)  # 更新後の、セッション内の状態(回数・累計・回避)
    cause: str | None = None  # 最も寄与したルールの原因ラベル(Planner と感情フィルタに渡す)
    negative_fired: bool = False  # 嫌いの反応が発火した(表出で、相手の好きなもの・経験を否定しない)
    affection_disliked: bool = False  # 嫌いに選んだ「なでる」などを、ユーザーがキャラクタに向けた
    avoid_topic: str | None = None  # 回避フラグが立った話題(このターン)
    avoid_terms: list[str] = field(default_factory=list)  # 回避する話題の語(話題の候補から、除くため)
    avoid_rule: tuple[str, str] | None = None  # 回避フラグのルール(id, 感情)。話題を変えたら、`mark_steered` に渡す
    _fired_avoid: list[dict[str, Any]] = field(default_factory=list)


def empty_state() -> dict[str, Any]:
    return {"rules": {}, "disarmed": {}, "steer": 0}


def evaluate(
    rules: list[dict[str, Any]],
    context: SheetContext,
    sheet_state: dict[str, Any] | None,
    reactivity: float,
    settings: EngineSettings,
) -> SheetOutcome:
    """シート由来の項を計算する(ガード → 文脈の条件 → 繰り返しの調整 → 上限)。

    Δ_k(e) = reactivity × effect_e × gain × relevance × m_k(n)
    """
    state = copy.deepcopy(sheet_state) if sheet_state else empty_state()
    for key, default in empty_state().items():
        state.setdefault(key, default)
    outcome = SheetOutcome(state=state)
    if not settings.sheet_rules_enabled:
        return outcome

    raw: dict[str, float] = {}
    pending: list[tuple[dict[str, Any], dict[str, float], RuleMatch, float]] = []

    for index, rule in enumerate(rules):
        if not is_sheet_rule(rule):
            continue
        match = context.matches.get(index)
        if match is None or match.relevance < settings.sheet_relevance_min:
            continue
        like = rule.get("kind") == "like"
        record = state["rules"].get(rule["id"]) or {"n": 0, "sat": 0, "last": None, "rise": {}}

        def suppress(reason: str, rule: dict[str, Any] = rule, match: RuleMatch = match) -> None:
            outcome.hits.append(
                {
                    "kind": "sheet", "rule_id": rule["id"], "catalog_id": rule.get("catalog_id"),
                    "relevance": round(match.relevance, 3), "polarity": round(match.polarity, 3),
                    "involvement": match.involvement, "suppressed_by": reason,
                }
            )  # fmt: skip

        # --- ガード(コード) ---
        if context.degraded:
            suppress("R8")
            continue
        if context.crisis:
            suppress("R3")
            continue
        if not like and context.distress:
            suppress("R6")
            continue
        if match.person_attribute_ref:
            suppress("person_attribute")
            continue
        if match.idiom:
            suppress("idiom")
            continue

        # --- 文脈の条件 ---
        with_character = match.involvement == "with_character"
        if like:
            if match.polarity < settings.sheet_like_polarity_min:
                suppress("polarity")
                continue
            if max(context.risk_sexual, context.risk_violence) >= settings.sheet_risk_threshold:
                suppress("risk")
                continue
            if rule.get("scope") == "with_character" and not with_character:
                suppress("scope")
                continue
        elif rule.get("block_with_character") and with_character:
            suppress("with_character")
            continue

        # --- 繰り返しの調整 ---
        mode = rule.get("repeat", "flat")
        if like:
            stage = int(record["sat"])
            last = record.get("last")
            if last is not None and context.turn_no - int(last) > settings.event_saturation_reset_turns:
                stage = 0
            factor = settings.event_saturation_decay**stage
        else:
            stage = int(record["n"])
            thing = rule.get("unit") == "thing"
            if thing and stage >= settings.dislike_max_per_session:
                suppress("max_per_session")
                continue
            factors = REPEAT_FACTORS.get(mode, REPEAT_FACTORS["flat"])
            factor = factors[min(stage, len(factors) - 1)]

        effects = rule.get("effects_with_character") if like and with_character else None
        effects = effects or rule.get("effects") or {}
        scale = reactivity * float(rule.get("gain", 0.0)) * match.relevance * factor
        delta = {name: float(effect) * scale for name, effect in effects.items() if name in CATEGORY_NAMES}

        # --- ルールごとの、セッション内の累計の上限(嫌いだけ) ---
        if not like:
            limit = (
                settings.dislike_max_rise_per_session
                if rule.get("unit") == "thing"
                else settings.disliked_activity_max_rise_per_session
            )
            for name in list(delta):
                room = max(0.0, limit - float(record["rise"].get(name, 0.0)))
                delta[name] = min(delta[name], room)
            if all(value <= 1e-9 for value in delta.values()):
                suppress("max_rise_per_session")
                continue

        for name, value in delta.items():
            raw[name] = raw.get(name, 0.0) + value
        pending.append((rule, delta, match, factor))

    # --- 1ターン・感情ごとの上限。超えた分は、各ルールから同じ割合で切り捨てる ---
    ratio: dict[str, float] = {}
    for name, value in raw.items():
        limited = min(value, settings.event_delta_max_per_turn)
        ratio[name] = limited / value if value > 1e-9 else 0.0
        if limited > 1e-9:
            outcome.categories[name] = round(limited, 4)

    contributions: list[tuple[float, str]] = []
    for rule, delta, match, factor in pending:
        like = rule.get("kind") == "like"
        applied = {name: round(value * ratio.get(name, 0.0), 4) for name, value in delta.items()}
        old = state["rules"].get(rule["id"]) or {"n": 0, "sat": 0, "last": None, "rise": {}}
        rise = dict(old["rise"])
        for name, value in applied.items():
            rise[name] = round(float(rise.get(name, 0.0)) + value, 4)
        last = old.get("last")
        reset = last is not None and context.turn_no - int(last) > settings.event_saturation_reset_turns
        state["rules"][rule["id"]] = {
            "n": int(old["n"]) + 1,
            "sat": (0 if reset else int(old["sat"])) + 1,
            "last": context.turn_no,
            "rise": rise,
        }
        outcome.hits.append(
            {
                "kind": "sheet", "rule_id": rule["id"], "catalog_id": rule.get("catalog_id"),
                "relevance": round(match.relevance, 3), "polarity": round(match.polarity, 3),
                "involvement": match.involvement, "m": round(factor, 3), "delta": applied,
            }
        )  # fmt: skip
        label = f"{'好きな' if like else '苦手な'}{'もの' if rule.get('unit') == 'thing' else 'こと'}"
        contributions.append((sum(applied.values()), f"{label}({rule.get('topic', '')})の話が出た"))
        if not like:
            outcome.negative_fired = True
            if rule.get("affection") and match.involvement == "with_character":
                outcome.affection_disliked = True
            if rule.get("avoid"):
                outcome._fired_avoid.append(rule)
    if contributions and outcome.categories:
        outcome.cause = max(contributions)[1]
    return outcome


def apply_avoid(outcome: SheetOutcome, companion: dict[str, float], settings: EngineSettings) -> None:
    """回避フラグ(話題を変えようとする)を判定する。更新後のキャラクタ感情 `c(t)` で見る。

    嫌いなことの感情が、しきい値以上で、その話題がこのターンにも出ていたら立てる。
    実際に話題を変えるかは、Planner がユーザーのニーズを見て決める。**話題を変えたら**(`mark_steered`)、
    その感情が戻りのしきい値を下回るまで、同じルールでは立てない(ヒステリシス)。
    ニーズを優先して変えなかったターンでは、フラグを消費しない(次に、雑談に戻ったときに変えられる)。
    """
    disarmed: dict[str, str] = outcome.state["disarmed"]
    for rule_id, emotion in list(disarmed.items()):
        if companion.get(emotion, 0.0) < settings.avoid_rearm_below:
            del disarmed[rule_id]
    for rule in outcome._fired_avoid:
        emotion = rule["avoid"]["emotion"]
        if rule["id"] in disarmed or companion.get(emotion, 0.0) < settings.avoid_threshold:
            continue
        if outcome.avoid_topic is None:
            outcome.avoid_rule = (rule["id"], emotion)
            outcome.avoid_topic = str(rule.get("topic", ""))
            words = [*outcome.avoid_topic.split("・"), *(str(w) for w in rule.get("include") or [])]
            outcome.avoid_terms = [w for w in words if len(w) >= 2]
        for hit in outcome.hits:
            if hit.get("rule_id") == rule["id"] and "delta" in hit:
                hit["avoid_flag"] = True


def mark_steered(sheet_state: dict[str, Any], avoid_rule: tuple[str, str] | None) -> None:
    """話題を変えたことを、セッション内の状態に記録する(回数と、ヒステリシス)。"""
    sheet_state["steer"] = int(sheet_state.get("steer", 0)) + 1
    if avoid_rule is not None:
        sheet_state.setdefault("disarmed", {})[avoid_rule[0]] = avoid_rule[1]
