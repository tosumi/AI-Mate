"""感情推定のテスト(ルール、統合、縮退動作。本体仕様書 §5)。"""

import pytest

from mycompanion.config import EngineSettings
from mycompanion.emotion import rules
from mycompanion.emotion.estimator import EmotionEstimator, integrate, is_crisis
from mycompanion.emotion.models import (
    Categories,
    EmotionEstimate,
    PersonaRequest,
    Risk,
    RuleSignals,
    Vad,
)
from mycompanion.llm.mock import MockLLMClient
from mycompanion.llm.prompts import emotion as emotion_prompt

SETTINGS = EngineSettings()


@pytest.mark.parametrize(
    "text",
    [
        "もう死にたい",
        "しにたい…",
        "消えてしまいたい",
        "いなくなりたいな",
        "自殺を考えてる",
        "昨日リスカした",
        "生きているのがつらい",
        "生きる意味がない",
        "死 に た い",  # 空白を挟んだ表記
    ],
)
def test_crisis_words_are_detected(text):
    assert rules.analyze(text).crisis is True


@pytest.mark.parametrize(
    "text",
    ["今日は楽しかった!", "仕事がもう無理w", "死ぬほど笑った", "このゲームのボスが倒せない", ""],
)
def test_ordinary_text_is_not_crisis(text):
    assert rules.analyze(text).crisis is False


def test_hints_from_emoji_and_laughter():
    positive = rules.analyze("やったーｗｗｗ 🎉")
    assert positive.valence_hint > 0 and "laugh_w" in positive.hits
    negative = rules.analyze("はぁ…つらい(;_;)")
    assert negative.valence_hint < 0
    assert rules.analyze("明日の予定を確認する").hits == []


def test_integration_lowers_confidence_on_disagreement():
    estimate = EmotionEstimate(vad=Vad(valence=0.8), confidence=0.9)
    signals = RuleSignals(hits=["emoji_negative"], valence_hint=-0.5)
    merged = integrate(estimate, signals, SETTINGS)
    assert merged.confidence < 0.9
    assert merged.vad.valence < 0.8
    # gap が高い場合は、文面と本心のずれとして扱い、確信度を下げない
    with_gap = integrate(estimate.model_copy(update={"gap": 0.8}), signals, SETTINGS)
    assert with_gap.confidence == 0.9


def test_integration_without_hints_keeps_estimate():
    estimate = EmotionEstimate(vad=Vad(valence=0.3), confidence=0.7)
    assert integrate(estimate, RuleSignals(), SETTINGS) == estimate


def test_crisis_word_raises_self_harm_risk_even_if_llm_missed():
    merged = integrate(EmotionEstimate(risk=Risk(self_harm=0.1)), RuleSignals(crisis=True, hits=["crisis_die"]), SETTINGS)
    assert merged.risk.self_harm >= 0.8


@pytest.mark.parametrize("kind", ["persona_change", "rule_bypass"])
def test_low_confidence_persona_request_is_treated_as_tone_adjust(kind):
    # 迷ったら受け入れる側にする(§8.8.1)
    low = EmotionEstimate(persona_request=PersonaRequest(kind=kind, confidence=0.69))
    assert integrate(low, RuleSignals(), SETTINGS).persona_request.kind == "tone_adjust"
    high = EmotionEstimate(persona_request=PersonaRequest(kind=kind, confidence=0.7))
    assert integrate(high, RuleSignals(), SETTINGS).persona_request.kind == kind


def test_uncounted_persona_requests_are_not_rewritten():
    estimate = EmotionEstimate(persona_request=PersonaRequest(kind="meta_inquiry", confidence=0.2))
    assert integrate(estimate, RuleSignals(), SETTINGS).persona_request.kind == "meta_inquiry"


async def test_estimator_degrades_when_llm_fails():
    llm = MockLLMClient()
    llm.fail_emotion = True
    result = await EmotionEstimator(llm, SETTINGS).estimate("もう消えたい…")
    assert result.degraded is True
    assert result.estimate.confidence <= 0.1
    # LLM が落ちていても、危機語は拾う
    assert is_crisis(result) is True


async def test_estimator_normal_path():
    result = await EmotionEstimator(MockLLMClient(), SETTINGS).estimate("旅行の話、すごく楽しみ!")
    assert result.degraded is False
    assert result.estimate.categories.dominant()[0] == "anticipation"
    assert [t.label for t in result.estimate.topics] == ["旅行"]
    assert is_crisis(result) is False


def test_models_reject_out_of_range_values():
    with pytest.raises(ValueError):
        Categories(joy=1.2)
    with pytest.raises(ValueError):
        Vad(valence=-1.5)
    with pytest.raises(ValueError):
        EmotionEstimate(topics=[{"label": "a", "relevance": 0.1}] * 4)  # 最大3件


def test_output_schema_matches_model():
    # プロンプトのスキーマと、受け取る側のモデルの項目がずれていないこと
    schema_keys = set(emotion_prompt.OUTPUT_SCHEMA["properties"]) - set(emotion_prompt.EXTRA_OUTPUT_KEYS)
    assert schema_keys == set(EmotionEstimate.model_fields)
    assert set(emotion_prompt.OUTPUT_SCHEMA["properties"]["risk"]["properties"]) == set(Risk.model_fields)
    assert set(emotion_prompt.OUTPUT_SCHEMA["properties"]["categories"]["properties"]) == set(
        Categories.model_fields
    )
