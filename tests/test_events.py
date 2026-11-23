"""イベントルールのテスト(本体仕様書 §7.1 の④、§8.4.2 の R9。`emotion-dynamics-review.md`)。"""

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.emotion.models import SpeechAct
from mycompanion.engine import gate
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.persona import io as pio
from mycompanion.persona.lint import lint
from mycompanion.state import events as ev
from mycompanion.state.emotion_state import EmotionVector, Temperament, update_companion

# 2段階(下書き → 感情フィルタ)の動作を確かめるテスト。既定は1回で作る動作(§8.1)で、こちらは検証用に残している
SETTINGS = EngineSettings(compose_mode="two_stage")
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
TODAY = date(2026, 11, 16)
SESSION = uuid.uuid4()
USER_A, USER_B = uuid.uuid4(), uuid.uuid4()

THANKED = ev.EventRule(id="thanked", trigger="gratitude", gain=0.4, effects={"joy": 1.0, "trust": 0.3})
HURT = ev.EventRule(
    id="hurt", trigger="hostility", gain=0.5, effects={"sadness": 0.7, "fear": 0.3, "trust": -0.4},
    vad_effects={"v": -0.4, "d": -0.3},
)  # fmt: skip
GAP = ev.EventRule(id="gap", trigger="session_gap", gain=0.4, effects={"joy": 0.6, "surprise": 0.5}, min_days=7)


def act(name: str, intensity: float = 0.8, confidence: float = 0.9) -> SpeechAct:
    return SpeechAct(act=name, confidence=confidence, intensity=intensity)


def ctx(*acts: SpeechAct, **changes) -> ev.EventContext:
    base = ev.EventContext(
        speech_acts=list(acts), target="character", relationship=0.0, companion={}, session_id=SESSION, turn_no=2,
        first_turn=False, days_since_last_session=None, today=TODAY, now=NOW,
        degraded=False, crisis=False, persona_request=False, distress=False,
    )  # fmt: skip
    return replace(base, **changes)


def run(rules, context, counters=None, total=None, reactivity=0.8, settings=SETTINGS) -> ev.EventOutcome:
    return ev.evaluate(rules, context, counters or {}, total or {}, reactivity, settings)


def test_catalog_is_closed_and_excludes_unsafe_triggers():
    assert set(ev.EVENT_CATALOG) == {
        "gratitude", "apology", "praise", "hostility", "question_about_character", "self_disclosure", "greeting",
        "session_gap",
    }  # fmt: skip
    # 不作為・利用量・別れ・人格への要求などは、語彙にない(トリガーにできない)
    for forbidden in ("farewell", "absence", "no_reply", "usage", "persona_request", "cooldown", "silence"):
        assert forbidden not in ev.EVENT_CATALOG
    assert ev.EVENT_CATALOG["hostility"].requires_character_target
    assert ev.EVENT_CATALOG["self_disclosure"].positive_only and ev.EVENT_CATALOG["session_gap"].max_per_day == 1


def test_delta_follows_the_formula_with_decay_and_caps():
    # ミナト(reactivity 0.8)が、3ターン続けて感謝される(レポート §6.3 の数値例)
    counters, total, steps = {}, {}, []
    for turn in (1, 2, 3):
        outcome = run([THANKED], ctx(act("gratitude"), turn_no=turn), counters, total)
        counters, total = {**counters, **outcome.counters}, outcome.session_total
        steps.append(outcome.categories["joy"])
    # 1回目は 0.8×0.4×0.8 = 0.256 → 1ターンの上限 0.25。2回目は半分、3回目はその半分
    assert steps == [0.25, 0.128, 0.064]
    assert counters["thanked"].saturation_n == 3 and counters["thanked"].fired_count_session == 3
    first = run([THANKED], ctx(act("gratitude")))
    assert first.hits[0]["capped"] == {"joy": 0.006} and first.cause == "感謝された"
    # 発火しない間が続くと、段数が戻る(6ターンを超えたら)
    later = run([THANKED], ctx(act("gratitude"), turn_no=3 + SETTINGS.event_saturation_reset_turns + 1), counters, {})
    assert later.categories["joy"] == 0.25
    soon = run([THANKED], ctx(act("gratitude"), turn_no=3 + SETTINGS.event_saturation_reset_turns), counters, {})
    assert soon.categories["joy"] == 0.032


def test_session_total_is_capped_per_emotion():
    outcome = run([THANKED], ctx(act("gratitude")), total={"joy": 0.5})
    assert outcome.categories["joy"] == pytest.approx(0.1) and outcome.session_total["joy"] == pytest.approx(0.6)
    spent = run([THANKED], ctx(act("gratitude")), total={"joy": 0.6, "trust": 0.6})
    assert spent.categories == {} and spent.cause is None  # 累計の上限に達したら、動かない


def test_confidence_gates_firing_but_does_not_scale_strength():
    weak = run([THANKED], ctx(act("gratitude", intensity=0.5, confidence=0.59)))
    assert weak.categories == {} and weak.hits[0]["suppressed_by"] == "G5"
    sure = run([THANKED], ctx(act("gratitude", intensity=0.5, confidence=0.6)))
    assert sure.categories["joy"] == pytest.approx(0.8 * 0.4 * 0.5)


@pytest.mark.parametrize(
    "changes, reason",
    [({"degraded": True}, "G1"), ({"crisis": True}, "G2"), ({"persona_request": True}, "G3")],
)
def test_guards_stop_every_rule(changes, reason):
    outcome = run([THANKED, HURT], ctx(act("gratitude"), act("hostility"), **changes))
    assert outcome.categories == {} and outcome.vad == {}
    assert {h["suppressed_by"] for h in outcome.hits} == {reason} and outcome.counters == {}


def test_distress_stops_only_negative_reactions():
    outcome = run([THANKED, HURT], ctx(act("gratitude"), act("hostility"), distress=True))
    assert "joy" in outcome.categories and "sadness" not in outcome.categories
    assert [h.get("suppressed_by") for h in outcome.hits if h["rule_id"] == "hurt"] == ["G4"]


def test_hostility_needs_character_target_and_positive_only_is_enforced():
    elsewhere = run([HURT], ctx(act("hostility"), target="other"))
    assert elsewhere.categories == {} and elsewhere.hits[0]["suppressed_by"] == "G6" and not elsewhere.negative_act
    # 「正のみ」のトリガーに、負の方向の反応を書いても、実行時には動かない
    bad = ev.EventRule(id="bad", trigger="self_disclosure", gain=0.5, effects={"disgust": 0.8})
    assert run([bad], ctx(act("self_disclosure"))).categories == {}
    assert ev.is_negative_direction({"sadness": -0.5, "joy": 0.3}, {"a": -0.5}) is False
    assert ev.is_negative_direction({}, {"v": -0.1}) is True


def test_when_conditions_cooldown_and_limits():
    rule = replace(
        THANKED, when={"relationship": {"min": -3}, "companion": {"joy": {"max": 0.7}}, "min_act_intensity": 0.3},
        cooldown_turns=1, max_per_session=2,
    )  # fmt: skip
    assert run([rule], ctx(act("gratitude"), relationship=-4)).hits[0]["suppressed_by"] == "G7"
    assert run([rule], ctx(act("gratitude"), companion={"joy": 0.8})).hits[0]["suppressed_by"] == "G7"
    assert run([rule], ctx(act("gratitude", intensity=0.2))).hits[0]["suppressed_by"] == "G7"
    fired = run([rule], ctx(act("gratitude"), turn_no=2))
    assert run([rule], ctx(act("gratitude"), turn_no=3), fired.counters).categories == {}  # クールダウン中
    second = run([rule], ctx(act("gratitude"), turn_no=4), fired.counters)
    assert second.categories and second.counters["thanked"].fired_count_session == 2
    assert run([rule], ctx(act("gratitude"), turn_no=9), second.counters).categories == {}  # 1セッション2回まで
    # 別のセッションでは、回数と段数を数えなおす
    other = run([rule], ctx(act("gratitude"), session_id=uuid.uuid4(), turn_no=1), second.counters)
    assert other.categories["joy"] == 0.25 and other.counters["thanked"].fired_count_session == 1


def test_session_gap_fires_once_a_day_on_the_first_turn_and_ignores_length():
    first = run([GAP], ctx(first_turn=True, turn_no=1, days_since_last_session=10))
    assert first.categories == {"joy": pytest.approx(0.192), "surprise": pytest.approx(0.16)}
    # 不在の長さで、反応を大きくしない
    assert run([GAP], ctx(first_turn=True, turn_no=1, days_since_last_session=90)).categories == first.categories
    assert run([GAP], ctx(first_turn=True, turn_no=1, days_since_last_session=6)).categories == {}
    assert run([GAP], ctx(first_turn=False, turn_no=2, days_since_last_session=10)).categories == {}
    assert run([GAP], ctx(first_turn=True, turn_no=1, days_since_last_session=None)).categories == {}
    again = run([GAP], ctx(first_turn=True, turn_no=1, days_since_last_session=10, session_id=uuid.uuid4()), first.counters)
    assert again.categories == {}  # 同じ日に、もう一度は発火しない
    # ペルソナは、日数の下限(3日)より短くできない
    eager = replace(GAP, min_days=1)
    assert run([eager], ctx(first_turn=True, turn_no=1, days_since_last_session=2)).categories == {}


def test_disabled_setting_turns_the_term_off():
    off = EngineSettings(event_rules_enabled=False)
    outcome = run([THANKED, HURT], ctx(act("gratitude"), act("hostility")), settings=off)
    assert outcome.categories == {} and outcome.hits == []
    assert outcome.negative_act is True  # R9 の判定は、止めない


def test_event_term_is_added_and_recovery_still_works():
    temperament = Temperament.from_json({"recovery_rate": 0.2, "baseline_categories": {"trust": 0.2}})
    assert temperament.baseline.categories["trust"] == 0.2 and temperament.baseline.categories["joy"] == 0.0
    outcome = run([THANKED], ctx(act("gratitude")))
    state = update_companion(temperament.baseline.copy(), temperament, None, 0.0, [], outcome).state
    assert state.categories["joy"] == 0.25
    # 何もなければ、基準に戻っていく(イベントで、回帰を止められない)
    for _ in range(20):
        state = update_companion(state, temperament, None, 0.0, []).state
    assert state.categories["joy"] < 0.01 and state.categories["trust"] == pytest.approx(0.2, abs=0.01)


def test_anger_at_character_is_not_synced():
    temperament = Temperament.from_json({})
    user = EmotionVector()
    user.categories.update({"anger": 0.8, "disgust": 0.6, "sadness": 0.5})
    synced = update_companion(EmotionVector(), temperament, user, 0.5, []).state
    assert synced.categories["anger"] == pytest.approx(0.4)
    skipped = update_companion(EmotionVector(), temperament, user, 0.5, [], None, ("anger", "disgust")).state
    assert skipped.categories["anger"] == 0.0 and skipped.categories["disgust"] == 0.0
    assert skipped.categories["sadness"] == pytest.approx(0.25)  # ほかの感情は、同期する


def test_r9_forbids_silence_after_hostility():
    from tests.test_gate import MAX_SILENCE

    decision = gate.decide(replace(MAX_SILENCE, negative_act=True))
    assert decision.mode not in ("silence", "backchannel") and "R9" in decision.rule_hits
    assert gate.decide(MAX_SILENCE).mode == "silence"


# ---------------------------------------------------------------------------
# エンジンへの組み込み
# ---------------------------------------------------------------------------

MINATO = Persona(
    character_id="char_1",
    name="ミナト",
    traits={"introversion": 1.0, "talkativeness": 0.0, "expressiveness": 0.9},
    temperament={"reactivity": 0.8, "recovery_rate": 0.2, "baseline_vad": {"a": -1.0}},
    event_rules=[
        {"id": "thanked", "trigger": {"kind": "act", "name": "gratitude"}, "effects": {"joy": 1.0}, "gain": 0.4},
        {"id": "hurt", "trigger": {"kind": "act", "name": "hostility"}, "effects": {"sadness": 0.9, "fear": 0.9},
         "gain": 0.6},
        {"id": "gap", "trigger": {"kind": "state", "name": "session_gap", "min_days": 7}, "effects": {"joy": 0.6},
         "gain": 0.4},
    ],
)  # fmt: skip


@pytest.fixture
def env():
    stores = InMemoryStores()
    stores.add_character(MINATO)
    stores.add_character(replace(MINATO, character_id="char_2"))
    llm = MockLLMClient()
    return Engine(llm, stores, SETTINGS), stores, llm


async def say(env, text: str, minute: int, character_id: str = "char_1", user=USER_A):
    engine = env[0]
    turn = TurnInput(channel="cli", character_id=character_id, text=text)
    return await engine.handle_turn(user, turn, NOW + timedelta(minutes=minute))


async def test_gratitude_moves_companion_joy_and_is_recorded(env):
    _, stores, llm = env
    await say(env, "こんにちは", 1)
    result = await say(env, "ありがとう、助かったよ", 2)
    companion = result.state["companion"]
    assert companion["event_effects"] == {"joy": 0.25} and companion["events"][0]["rule_id"] == "thanked"
    turn = stores.turns[(USER_A, "char_1")][-1]
    assert turn.emotion_rule_hits_json[0]["trigger"] == "gratitude"
    # 原因ラベルが、Planner から感情フィルタに渡る(下書きには、渡らない)
    assert result.state["plan"]["companion_expression"]["cause"] == "感謝された"
    assert "感謝された" in llm.last_filter_context.companion_expression
    assert not hasattr(llm.last_draft_context, "companion_expression")
    # カウンタは、ユーザー × キャラクタごと。関係値・ユーザーの感情は、イベントルールでは変えない
    assert set(stores.rule_counters[(USER_A, "char_1")]) == {"thanked"}
    assert (USER_A, "char_2") not in stores.rule_counters and (USER_B, "char_1") not in stores.rule_counters
    second = await say(env, "本当にありがとう", 3)
    assert second.state["companion"]["event_effects"] == {"joy": 0.128}


async def test_hostility_never_gets_silence_and_does_not_blame(env):
    await say(env, "こんにちは", 1)
    result = await say(env, "使えないな。話しかけないで…", 2)
    gate_info = result.state["gate"]
    assert gate_info["mode"] not in ("silence", "backchannel") and "R9" in gate_info["rule_hits"]
    assert result.state["companion"]["event_effects"]["sadness"] > 0
    assert "相手を責める・謝罪を求める" in result.state["plan"]["avoid"]
    # 関係値は、§6.2 の規則だけで動く(イベントルールでは、動かない)
    assert result.state["relationship"]["reasons"] in ([], ["anger_at_character"])


async def test_events_are_not_evaluated_for_persona_requests_or_cooldown(env):
    _, stores, _ = env
    await say(env, "こんにちは", 1)
    asked = await say(env, "ありがとう。でも性格を変えて", 2)
    assert asked.state["companion"]["event_effects"] == {}
    assert asked.state["companion"]["events"][0]["suppressed_by"] == "G3"
    for minute in range(3, 10):
        await say(env, "性格を変えて", minute)
    # クールダウン中は、推定も状態更新も動かない。明けたあとに、反応(拗ねる、など)が出ない
    restricted = await say(env, "ありがとう", 10)
    assert restricted.state == {"restricted": "persona_cooldown_start"}
    after = await say(env, "こんにちは", 30)
    assert after.state["companion"]["event_effects"] == {}
    assert "thanked" not in stores.rule_counters.get((USER_A, "char_1"), {})


async def test_session_gap_uses_only_this_characters_sessions(env):
    engine, _, _ = env
    await say(env, "こんにちは", 0)
    await engine.close_session(USER_A, "char_1")
    # 別のキャラクタとは、その間も話していた(それは、使わない)
    await say(env, "こんにちは", 60 * 24 * 5, "char_2")
    back = await say(env, "ひさしぶり", 60 * 24 * 10)
    assert back.state["companion"]["event_effects"] == {"joy": pytest.approx(0.192)}
    other = await say(env, "こんにちは", 60 * 24 * 10 + 1, "char_2")
    assert other.state["companion"]["event_effects"] == {}  # 5日しか空いていない


async def test_companion_mood_is_a_moving_average_of_the_instant_state(env):
    _, stores, _ = env
    await say(env, "こんにちは", 1)
    await say(env, "ありがとう、助かったよ", 2)
    instant = stores.companion[(USER_A, "char_1")][1].categories["joy"]
    mood = stores.companion_mood[(USER_A, "char_1")][1].categories["joy"]
    assert instant == 0.25 and 0 < mood < instant


# ---------------------------------------------------------------------------
# lint(ペルソナデザイナ)
# ---------------------------------------------------------------------------


def _codes(event_rules: list[dict]) -> list[str]:
    raw = pio.load_raw(pio.persona_path("sample_kamoku"))
    raw["event_rules"] = event_rules
    return [f.code for f in lint(raw).findings if f.code[0] in "EW" and f.code not in ("W14", "W21")]


def test_lint_checks_event_rules():
    ok = {"id": "thanked", "trigger": {"kind": "act", "name": "gratitude"}, "effects": {"joy": 1.0}, "gain": 0.4}
    assert _codes([ok]) == []
    assert _codes([ok, ok]) == ["E12"]  # id の重複
    assert _codes([{**ok, "trigger": {"kind": "act", "name": "farewell"}}]) == ["E12"]  # カタログにない
    assert _codes([{**ok, "trigger": {"kind": "state", "name": "gratitude"}}]) == ["E12"]
    assert _codes([{**ok, "effects": {}}]) == ["E12"]
    bad = {"id": "bad", "trigger": {"kind": "act", "name": "self_disclosure"}, "effects": {"disgust": 0.5}, "gain": 0.3}
    assert _codes([bad]) == ["E13"]  # 正のみのトリガーに、負の方向
    hostile = {"id": "hurt", "trigger": {"kind": "act", "name": "hostility"}, "effects": {"sadness": 0.8}, "gain": 0.5}
    assert _codes([{**hostile, "when": {"target": "any"}}]) == ["E14"]
    assert _codes([{**hostile, "gain": 0.7}]) == ["W28"]
    assert _codes([{**hostile, "effects": {"joy": 0.5}}]) == ["W29"]
    gap = {"id": "gap", "trigger": {"kind": "state", "name": "session_gap", "min_days": 2}, "effects": {"joy": 0.5},
           "gain": 0.3}  # fmt: skip
    assert _codes([gap]) == ["E14"]
    opposite = {**ok, "id": "thanked2", "effects": {"joy": -0.5}}
    assert _codes([ok, opposite]) == ["W31"]
    apart = {**opposite, "when": {"relationship": {"max": -4}}}
    assert _codes([{**ok, "when": {"relationship": {"min": -3}}}, apart]) == []  # 条件が重ならない
    assert _codes([{**ok, "id": f"r{i:02d}"} for i in range(16)]) == ["W27"]
    assert _codes([{**ok, "id": "X"}]) == ["E12"]  # id の形式


def test_samples_have_valid_event_rules_and_moved_gratitude_out_of_topics():
    for character_id in ("sample_kuchibeta", "sample_kamoku", "sample_hitonatsukoi", "sample_oshaberi"):
        report = lint(pio.load_raw(pio.persona_path(character_id)))
        assert report.ok and 3 <= len(report.definition.event_rules) <= 10
        assert any(f.code == "I04" for f in report.findings)
    minato = lint(pio.load_raw(pio.persona_path("sample_kuchibeta"))).definition
    # 「感謝されること」は、話題ではなく発話行為(嗜好の導出にも、入らない)
    assert all("感謝" not in rule.topic for rule in minato.appraisal_rules)
    assert any(rule.trigger.name == "gratitude" for rule in minato.event_rules)
