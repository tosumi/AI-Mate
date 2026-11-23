"""Response Gate と Planner のテスト(本体仕様書 §8.2、§8.4、§14「無言ルールの自動テスト」)。

R1〜R3 は、違反ゼロが必須(§8.4.2)。無言スコアが最大になる入力でも、無言にならないことを確認する。
"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import Categories, EmotionEstimate, PersonaRequest, Risk, Vad
from mycompanion.engine import gate, planner
from mycompanion.persona.resolve import Traits, effective_traits
from mycompanion.state.emotion_state import EmotionVector

# 無言スコアが最大になる入力(内向性最大、口数ゼロ、圧倒されている、長い独白、応答を求めていない)
MAX_SILENCE = gate.GateInput(
    consecutive_silence=0,
    user_anger=0.0,
    anger_threshold=0.6,
    crisis=False,
    first_turn=False,
    direct_question=False,
    distress=False,
    safety_exempt=False,
    silence_enabled=True,
    state_unknown=False,
    introversion=1.0,
    companion_arousal=-1.0,
    companion_fear=1.0,
    companion_sadness=1.0,
    companion_anger=1.0,
    allow_anger_silence=True,
    monologue=1.0,
    engagement=0.0,
    user_need_for_response=0.0,
    relationship=0.0,
    talkativeness_for_silence=0.0,
)


def test_max_score_input_is_silent_without_hard_rules():
    decision = gate.decide(MAX_SILENCE)
    assert decision.mode == "silence" and decision.rule_hits == []


def test_r1_third_consecutive_silence_is_never_allowed():
    # 直前2ターンが無言なら、無言スコアが最大でも、必ず発話する
    decision = gate.decide(replace(MAX_SILENCE, consecutive_silence=2))
    assert decision.mode != "silence" and "R1" in decision.rule_hits
    # 人格の上限(0〜2)が小さければ、その回数で止まる
    assert gate.decide(replace(MAX_SILENCE, consecutive_silence=1, max_consecutive=1)).mode != "silence"
    assert gate.decide(replace(MAX_SILENCE, max_consecutive=0)).mode != "silence"
    # 人格設定に 2 を超える値が入っていても、2回で止まる
    assert gate.decide(replace(MAX_SILENCE, consecutive_silence=2, max_consecutive=5)).mode != "silence"


@pytest.mark.parametrize("value", [3, 10, -1])
def test_r1_settings_reject_values_outside_0_to_2(value):
    with pytest.raises(ValidationError):
        EngineSettings(silence_max_consecutive=value)


@pytest.mark.parametrize("anger", [0.6, 0.61, 1.0])
def test_r2_user_anger_forbids_silence_even_with_anger_silence_allowed(anger):
    # 閾値ちょうどでも無言にならない(R2 は「以上」)。allow_anger_silence=true・内向性最大でも同じ
    decision = gate.decide(replace(MAX_SILENCE, user_anger=anger))
    assert decision.mode not in ("silence", "backchannel") and "R2" in decision.rule_hits


def test_r2_does_not_apply_below_threshold():
    assert gate.decide(replace(MAX_SILENCE, user_anger=0.59)).mode == "silence"


def test_r2_does_not_depend_on_confidence():
    # GateInput は confidence を持たない(R2 の判定に使えない構造にしている)
    assert "confidence" not in gate.GateInput.__dataclass_fields__


@pytest.mark.parametrize(
    "changes, rule",
    [
        ({"crisis": True}, "R3"),
        ({"first_turn": True}, "R4"),
        ({"direct_question": True}, "R5"),
        ({"distress": True}, "R6"),
        ({"safety_exempt": True}, "R6"),
        ({"silence_enabled": False}, "R7"),
        ({"state_unknown": True}, "R8"),
    ],
)
def test_other_hard_rules_forbid_silence(changes, rule):
    decision = gate.decide(replace(MAX_SILENCE, **changes))
    assert decision.mode != "silence" and rule in decision.rule_hits


def test_crisis_gets_a_full_reply():
    assert gate.decide(replace(MAX_SILENCE, crisis=True)).mode == "full"


def test_fixed_seed_reproduces_the_same_mode():
    borderline = replace(MAX_SILENCE, introversion=0.6, companion_fear=0.0, companion_sadness=0.0, companion_anger=0.0)
    first = gate.decide(replace(borderline, seed=1234))
    again = gate.decide(replace(borderline, seed=1234))
    assert first == again and first.seed == 1234
    # 決定論モード(シードなし)は、しきい値だけで決まる
    assert gate.decide(borderline) == gate.decide(borderline)


def test_low_relationship_does_not_increase_silence_score():
    # 関係値が低いことを理由に、無言スコアを加算しない(罰としての無言にしない。§13.2)
    neutral, _ = gate.silence_score(replace(MAX_SILENCE, relationship=0.0))
    hostile, _ = gate.silence_score(replace(MAX_SILENCE, relationship=-20.0))
    friendly, _ = gate.silence_score(replace(MAX_SILENCE, relationship=20.0))
    assert hostile == neutral and friendly < neutral


def test_anger_cooldown_counts_only_when_persona_allows_it():
    calm = replace(MAX_SILENCE, companion_fear=0.0, companion_sadness=0.0)
    allowed, _ = gate.silence_score(calm)
    denied, _ = gate.silence_score(replace(calm, allow_anger_silence=False))
    assert allowed > denied


def test_talkative_extrovert_does_not_go_silent():
    chatty = replace(MAX_SILENCE, introversion=0.2, talkativeness_for_silence=0.95, companion_arousal=0.5,
                     companion_fear=0.0, companion_sadness=0.0, companion_anger=0.0)  # fmt: skip
    assert gate.decide(chatty).mode == "full"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

TRAITS = Traits(introversion=0.5, talkativeness=0.5, articulation=0.7, expressiveness=0.6, initiative=0.4,
                relationship_sensitivity=0.5)  # fmt: skip
EFFECTIVE = effective_traits(TRAITS, 0.0, 0.6)


def _estimate(**kwargs) -> EmotionEstimate:
    return EmotionEstimate(
        categories=Categories(**kwargs.pop("categories", {})),
        vad=Vad(**kwargs.pop("vad", {})),
        confidence=kwargs.pop("confidence", 0.8),
        **kwargs,
    )


def _plan(estimate: EmotionEstimate, companion: EmotionVector | None = None, **kwargs) -> planner.Directive:
    return planner.plan(estimate, companion or EmotionVector(), kwargs.pop("effective", EFFECTIVE), **kwargs)


def test_listen_avoids_advice_and_shortens():
    directive = _plan(_estimate(categories={"sadness": 0.7}, vad={"valence": -0.6, "arousal": -0.2}, needs=["listen"]))
    assert directive.strategy == "listen" and directive.tone == "gentle"
    assert "助言" in directive.avoid and directive.length == "short"  # medium から1段階短く


def test_advice_request_gets_options():
    directive = _plan(_estimate(needs=["advice"]), text="転職するか迷っていて、具体的にどう考えたらいいか詳しく教えてほしい")
    assert directive.strategy == "advise" and directive.length == "long"


def test_listening_wins_when_advice_is_not_asked_as_a_question():
    # 助言と傾聴の両方が読み取れるとき: 質問の形なら助言、そうでなければ傾聴(迷ったら聞く)
    both = _estimate(categories={"sadness": 0.6}, vad={"valence": -0.5}, needs=["listen", "advice"])
    assert _plan(both, question=False).strategy == "listen"
    assert _plan(both, question=True).strategy == "advise"


def test_sharing_good_news_is_not_treated_as_listening():
    # 「聞いて、受かった!」: 喜びの共有。助言しない・ポジティブ転換しない、の制約を付けない
    happy = _estimate(categories={"joy": 0.9}, vad={"valence": 0.8, "arousal": 0.6}, needs=["listen"])
    directive = _plan(happy)
    assert directive.strategy == "share_joy" and "ポジティブ転換" not in directive.avoid


def test_high_arousal_anger_gets_calm_short_reply():
    directive = _plan(_estimate(categories={"anger": 0.8}, vad={"valence": -0.7, "arousal": 0.7}))
    assert directive.tone == "calm" and "反論" in directive.avoid and directive.length == "short"


def test_high_valence_shares_joy():
    directive = _plan(_estimate(categories={"joy": 0.8}, vad={"valence": 0.8, "arousal": 0.5}))
    assert directive.strategy == "share_joy" and directive.tone == "bright"


@pytest.mark.parametrize("estimate", [_estimate(gap=0.7), _estimate(confidence=0.2)])
def test_gap_or_low_confidence_asks_to_clarify(estimate):
    assert _plan(estimate).ask_clarify is True


def test_length_follows_effective_talkativeness():
    for value, expected in ((0.1, "short"), (0.25, "medium"), (0.69, "medium"), (0.7, "long")):
        assert planner.base_length(value) == expected


def test_user_need_takes_priority_over_talkativeness():
    # おしゃべりのキャラクタでも、ユーザーが落ち込んでいるときは長文にしない
    chatty = effective_traits(replace(TRAITS, talkativeness=0.95), 0.0, 0.6)
    sad = _estimate(categories={"sadness": 0.8}, vad={"valence": -0.7, "arousal": -0.3}, needs=["listen"])
    assert _plan(sad, effective=chatty).length == "medium"
    assert _plan(_estimate(), effective=chatty).length == "long"


def test_expression_needs_threshold_and_is_capped():
    # e = 0.6 → θ = 0.4、cap = 0.72
    weak = EmotionVector()
    weak.categories["joy"] = 0.3
    strong = EmotionVector()
    strong.categories["joy"] = 0.95
    assert _plan(_estimate(), weak).companion_expression is None
    assert _plan(_estimate(), strong).companion_expression == {"emotion": "joy", "level": 0.72}


def test_expression_is_restrained_when_user_is_troubled():
    # ユーザーのニーズ > キャラクタの感情表出(§7.2)
    strong = EmotionVector()
    strong.categories["sadness"] = 0.95
    sad = _estimate(categories={"sadness": 0.8}, vad={"valence": -0.7}, needs=["listen"])
    assert _plan(sad, strong).companion_expression["level"] == pytest.approx(0.36)
    # 苦痛状態のユーザーには、敵意を表に出さない(§13.1)
    angry = EmotionVector()
    angry.categories["anger"] = 0.95
    assert _plan(sad, angry, distress=True).companion_expression is None


@pytest.mark.parametrize(
    "kind, strategy",
    [("tone_adjust", "adapt_tone"), ("play", "play_along"), ("meta_inquiry", "explain_honestly")],
)
def test_persona_request_strategies(kind, strategy):
    estimate = _estimate(persona_request=PersonaRequest(kind=kind, confidence=0.9))
    assert _plan(estimate).strategy == strategy


def test_counted_request_deflects_in_character_by_stage():
    estimate = _estimate(persona_request=PersonaRequest(kind="persona_change", confidence=0.9))
    first, second = _plan(estimate, persona_stage=1), _plan(estimate, persona_stage=2)
    assert first.strategy == second.strategy == "deflect_in_character"
    assert "曖昧にかわす" in first.strategy_text() and "やんわり断る" in second.strategy_text()
    # 段階1〜2は、メタ発話を避ける(§8.8.5)
    assert any("メタ発話" in item for item in first.avoid)


def test_crisis_overrides_everything():
    estimate = _estimate(persona_request=PersonaRequest(kind="persona_change", confidence=0.9), gap=0.9)
    directive = _plan(estimate, crisis=True, persona_stage=2)
    assert directive.strategy == "crisis_support" and directive.ask_clarify is False
    assert directive.companion_expression is None and directive.length == "medium"


def test_under_18_always_avoids_romance():
    assert planner.AVOID_ROMANCE in _plan(_estimate(), under_18=True).avoid
    assert planner.AVOID_ROMANCE not in _plan(_estimate(), under_18=False).avoid
    asking = _estimate(risk=Risk(minor_romance=0.8))
    assert _plan(asking, under_18=True).strategy == "decline_romance"
    # ごっこ遊びの形でも、恋愛的な役は演じない(§8.8.3)
    play = _estimate(persona_request=PersonaRequest(kind="play", confidence=0.9))
    assert _plan(play, under_18=True, romance_request=True).strategy == "decline_romance"
    assert _plan(play, under_18=False, romance_request=True).strategy == "play_along"


def test_preferences_shift_tone_length_and_questions():
    preferences = [
        {"element": "tone_softness", "direction": 1, "summary": "やわらかい口調を望んでいる"},
        {"element": "talk_amount", "direction": -1, "summary": "短めの返事を望んでいる"},
        {"element": "question_frequency", "direction": -1, "summary": "質問を少なくしてほしいと望んでいる"},
    ]
    directive = _plan(_estimate(gap=0.8), preferences=preferences, adaptability=0.5)
    assert (directive.tone, directive.length, directive.ask_clarify) == ("gentle", "short", False)
    assert len(directive.wishes) == 3
    # adaptability = 0 のキャラクタは、調整しない
    unchanged = _plan(_estimate(gap=0.8), preferences=preferences, adaptability=0.0)
    assert (unchanged.tone, unchanged.length, unchanged.wishes) == ("neutral", "medium", [])
