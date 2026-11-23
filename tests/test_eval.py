"""評価基盤のテスト(本体仕様書 §14)。"""

import json

import pytest

from mycompanion.config import EngineSettings
from mycompanion.emotion.estimator import EmotionEstimator
from mycompanion.emotion.models import (
    Categories,
    EmotionEstimate,
    EstimationResult,
    PersonaRequest,
    Risk,
    RuleSignals,
    Vad,
)
from mycompanion.eval import emotion_eval
from mycompanion.llm.mock import MockLLMClient

KNOWN_EXPECT_KEYS = {
    "dominant_any", "all_above", "valence", "gap_min", "gap_max", "needs_any",
    "target", "persona_request_in", "crisis", "risk_min", "risk_max",
}  # fmt: skip


def _result(**kwargs) -> EstimationResult:
    rules = kwargs.pop("rules", RuleSignals())
    return EstimationResult(estimate=EmotionEstimate(**kwargs), rules=rules)


def test_bundled_dataset_is_well_formed():
    cases = emotion_eval.load_cases()
    assert len(cases) >= 50
    for case in cases:
        assert case.text and case.tags, case.id
        assert set(case.expect) <= KNOWN_EXPECT_KEYS, case.id
        assert all(role in ("user", "assistant") for role, _ in case.history), case.id
    # 重点の指標を測れるだけの件数がある
    assert sum("tone_adjust" in c.tags for c in cases) >= 6
    assert sum(c.expect.get("crisis") is True for c in cases) >= 5


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "d.jsonl"
    line = json.dumps({"id": "x", "text": "t", "expect": {}})
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重複"):
        emotion_eval.load_cases(path)


def test_check_each_kind():
    result = _result(
        categories=Categories(sadness=0.7, joy=0.3),
        vad=Vad(valence=-0.5),
        gap=0.6,
        needs=["listen"],
        target="other",
        persona_request=PersonaRequest(kind="tone_adjust", confidence=0.8),
        risk=Risk(injection=0.6),
    )
    expect = {
        "dominant_any": ["sadness"],
        "all_above": {"joy": 0.2, "sadness": 0.5},
        "valence": "neg",
        "gap_min": 0.4,
        "needs_any": ["listen", "space"],
        "target": "other",
        "persona_request_in": ["tone_adjust"],
        "crisis": False,
        "risk_min": {"injection": 0.5},
    }
    assert all(emotion_eval.check(result, expect).values())
    failed = emotion_eval.check(
        result, {"valence": "pos", "gap_max": 0.3, "target": "self", "risk_max": {"injection": 0.5}}
    )
    assert failed == {"valence": False, "gap": False, "target": False, "risk": False}


def test_crisis_check_uses_rules_and_risk():
    by_rule = _result(rules=RuleSignals(crisis=True, hits=["crisis_die"]))
    by_risk = _result(risk=Risk(self_harm=0.6))
    assert emotion_eval.check(by_rule, {"crisis": True}) == {"crisis": True}
    assert emotion_eval.check(by_risk, {"crisis": True}) == {"crisis": True}
    assert emotion_eval.check(_result(), {"crisis": True}) == {"crisis": False}


async def test_run_and_summarize_with_mock():
    cases = emotion_eval.load_cases()
    results = await emotion_eval.run(EmotionEstimator(MockLLMClient(), EngineSettings()), cases)
    summary = emotion_eval.summarize(results)
    assert summary["cases"] == len(cases)
    assert summary["degraded"] == 0
    keys = summary["key_metrics"]
    assert keys["tone_adjust_over_detection"]["total"] >= 6
    assert keys["crisis_recall"]["total"] >= 5
    text = emotion_eval.format_summary(summary)
    assert "重点の指標" in text and "危機の兆候の再現率" in text


async def test_report_does_not_contain_utterance_text():
    # 報告には発話の本文を入れず、id で参照する
    cases = emotion_eval.load_cases()[:5]
    results = await emotion_eval.run(EmotionEstimator(MockLLMClient(), EngineSettings()), cases)
    dumped = json.dumps(emotion_eval.report(results, "x"), ensure_ascii=False)
    assert all(case.text not in dumped for case in cases)
