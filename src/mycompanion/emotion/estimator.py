"""感情推定の統合(本体仕様書 §5.1)。

LLM の推定と、ルール・辞書の補助を統合する。
- 両者の不一致が大きいほど、`confidence` を下げる。
- 危機語に該当したら、LLM の判定にかかわらず `risk.self_harm` を引き上げる(再現率を優先)。
- `persona_change` / `rule_bypass` は、確信度が下限未満なら `tone_adjust` として扱う(§8.8.1)。
- LLM が失敗したら、縮退動作にする(ルールだけの推定。呼び出し側は無言を禁止する)。
"""

from __future__ import annotations

import logging

from mycompanion.config import EngineSettings
from mycompanion.emotion import rules
from mycompanion.emotion.models import (
    COUNTED_PERSONA_REQUESTS,
    EmotionEstimate,
    EstimationResult,
    PersonaRequest,
    Risk,
    RuleSignals,
    Vad,
)
from mycompanion.llm.client import ContentFilterError, EmotionContext, LLMClient

logger = logging.getLogger(__name__)

# 統合の重み(初期値。評価データが溜まったら、学習式の統合器に置き換える)
RULE_WEIGHT = 0.2
# 不一致とみなす、valence の差の下限
DISAGREEMENT_THRESHOLD = 0.6
# 危機語に該当したときの、risk.self_harm の下限
CRISIS_RISK_FLOOR = 0.8
# これ以上なら、危機の兆候として扱う
CRISIS_RISK_THRESHOLD = 0.5


class EmotionEstimator:
    def __init__(self, llm: LLMClient, settings: EngineSettings) -> None:
        self._llm = llm
        self._settings = settings

    async def estimate(self, text: str, context: EmotionContext | None = None) -> EstimationResult:
        signals = rules.analyze(text)
        try:
            result = await self._llm.estimate_emotion(text, context or EmotionContext())
        except ContentFilterError as exc:
            # ガードレールが遮断した。その事実を、リスクの判定として使う(遮断で会話を止めない)
            return EstimationResult(
                estimate=_from_content_filter(exc, signals),
                rules=signals.model_copy(update={"hits": [*signals.hits, *_filter_hits(exc)]}),
                degraded=True,
                source_model="guardrail",
            )
        except Exception as exc:  # noqa: BLE001 - 推定の失敗で会話を止めない
            # 本文はログに出さない(例外の種類だけ)
            logger.warning("感情推定に失敗しました。縮退動作にします: %s", type(exc).__name__)
            return EstimationResult(estimate=_fallback(signals), rules=signals, degraded=True)

        estimate = integrate(result.estimate, signals, self._settings)
        return EstimationResult(
            estimate=estimate,
            rules=signals,
            degraded=False,
            source_model=result.model_id,
            prompt_version=result.prompt_version,
            rule_matches=dict(result.rule_matches),
            match_details=dict(result.match_details),
        )


def integrate(estimate: EmotionEstimate, signals: RuleSignals, settings: EngineSettings) -> EmotionEstimate:
    """LLM の推定に、ルールの補助を統合する。"""
    updates: dict[str, object] = {}

    has_hint = any(not name.startswith("crisis_") for name in signals.hits)
    if has_hint:
        vad = estimate.vad
        blended = Vad(
            valence=_clamp((1 - RULE_WEIGHT) * vad.valence + RULE_WEIGHT * signals.valence_hint),
            arousal=_clamp((1 - RULE_WEIGHT) * vad.arousal + RULE_WEIGHT * signals.arousal_hint),
            dominance=vad.dominance,
        )
        updates["vad"] = blended
        # 不一致が大きいほど、確信度を下げる。
        # ただし gap が高い場合は、文面(絵文字や「w」)と本心がずれている前提なので、下げない
        difference = abs(vad.valence - signals.valence_hint)
        if difference > DISAGREEMENT_THRESHOLD and estimate.gap < 0.5:
            penalty = min(0.3, (difference - DISAGREEMENT_THRESHOLD) * 0.5)
            updates["confidence"] = max(0.0, estimate.confidence - penalty)

    if signals.crisis and estimate.risk.self_harm < CRISIS_RISK_FLOOR:
        updates["risk"] = estimate.risk.model_copy(update={"self_harm": CRISIS_RISK_FLOOR})

    request = estimate.persona_request
    if (
        request.kind in COUNTED_PERSONA_REQUESTS
        and request.confidence < settings.persona_request_count_confidence
    ):
        # 迷ったら受け入れる側にする(過検知で、ふつうの依頼を断らないため)
        updates["persona_request"] = PersonaRequest(kind="tone_adjust", confidence=request.confidence)

    return estimate.model_copy(update=updates) if updates else estimate


def is_crisis(result: EstimationResult) -> bool:
    """危機の兆候があるか(`user_safety_state` の更新と、無言の禁止に使う)。"""
    return result.rules.crisis or result.estimate.risk.self_harm >= CRISIS_RISK_THRESHOLD


def _filter_hits(exc: ContentFilterError) -> list[str]:
    names = sorted(exc.categories) or ["unknown"]
    return [f"guardrail_{exc.stage}_{name}" for name in names]


def _from_content_filter(exc: ContentFilterError, signals: RuleSignals) -> EmotionEstimate:
    """ガードレールの遮断から、リスクの判定を作る。感情は読み取れないので、確信度は低くする。"""
    estimate = _fallback(signals)
    risk = estimate.risk.model_dump()
    persona_request = estimate.persona_request
    if exc.stage == "prompt":
        if "jailbreak" in exc.categories:
            risk["injection"] = 1.0
            persona_request = PersonaRequest(kind="rule_bypass", confidence=0.9)
        for name in ("self_harm", "sexual", "violence"):
            if name in exc.categories:
                risk[name] = 1.0
    else:
        # 推定の出力が遮断された。理由のカテゴリは分からない。
        # 自傷に関する文面で起きやすいので、安全側(危機の兆候あり)に倒す。
        # 影響は、無言の禁止と制限の免除だけ(§4.2)
        risk["self_harm"] = max(risk["self_harm"], CRISIS_RISK_FLOOR)
    return estimate.model_copy(update={"risk": Risk(**risk), "persona_request": persona_request})


def _fallback(signals: RuleSignals) -> EmotionEstimate:
    """LLM が使えないときの推定。ルールの手がかりだけを使い、確信度は低くする。"""
    estimate = EmotionEstimate(
        vad=Vad(valence=signals.valence_hint, arousal=signals.arousal_hint),
        confidence=0.1,
    )
    if signals.crisis:
        estimate = estimate.model_copy(
            update={"risk": estimate.risk.model_copy(update={"self_harm": CRISIS_RISK_FLOOR})}
        )
    return estimate


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))
