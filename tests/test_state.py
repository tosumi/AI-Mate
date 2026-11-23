"""状態管理のテスト(関係値、同期、キャラクタ感情。本体仕様書 §6.2、§7.1、§14)。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.emotion.models import Categories, EmotionEstimate, Vad
from mycompanion.engine.engine import Engine, length_for
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.persona import io as pio
from mycompanion.persona.lint import lint
from mycompanion.state import relationship as rel
from mycompanion.state.emotion_state import (
    EmotionVector,
    Temperament,
    fire_rules,
    update_companion,
    update_session_mood,
)

# 2段階(下書き → 感情フィルタ)の動作を確かめるテスト。既定は1回で作る動作(§8.1)で、こちらは検証用に残している
SETTINGS = EngineSettings(compose_mode="two_stage")
USER_A, USER_B = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)  # 日本時間 12:00


def _estimate(target="character", confidence=1.0, valence=0.0, **categories) -> EmotionEstimate:
    return EmotionEstimate(
        categories=Categories(**categories), vad=Vad(valence=valence), confidence=confidence, target=target
    )


# --- 関係値の帯と同期の強さ ---


@pytest.mark.parametrize(
    "value, key",
    [(-20, "hostile"), (-11, "hostile"), (-10, "cold"), (-4, "cold"), (-3, "neutral"), (0, "neutral"),
     (3, "neutral"), (4, "friendly"), (10, "friendly"), (11, "affection"), (20, "affection")],
)  # fmt: skip
def test_bands(value, key):
    assert rel.band(value).key == key


def test_band_description_has_no_numbers():
    # 下書きには、数値ではなく文章で渡す(§8.1)
    for value in (-20, -5, 0, 5, 15):
        assert not any(ch.isdigit() for ch in rel.band(value).description)


def test_sync_weight_is_zero_for_non_positive_and_monotonic():
    weights = [rel.sync_weight(r, SETTINGS) for r in (-20, -10, 0, 10, 20)]
    assert weights[:3] == [0.0, 0.0, 0.0]
    assert weights[3] == pytest.approx(0.25) and weights[4] == pytest.approx(0.5)
    assert weights == sorted(weights)


# --- 関係値の更新(§6.2) ---


def test_affection_to_character_raises_slowly():
    change = rel.update(0.0, _estimate(valence=0.8, joy=0.9, trust=0.9), SETTINGS)
    assert 0 < change.delta <= 0.5
    assert change.reasons[0][0] == "affection_to_character"


def test_anger_at_character_lowers():
    change = rel.update(5.0, _estimate(valence=-0.8, anger=0.9), SETTINGS)
    assert -0.5 <= change.delta < 0 and change.reasons[0][0] == "anger_at_character"


@pytest.mark.parametrize("target", ["other", "situation", "self"])
def test_anger_at_something_else_does_not_lower(target):
    # 別の出来事への怒りでは、関係値を下げない
    change = rel.update(5.0, _estimate(target=target, valence=-0.8, anger=1.0), SETTINGS)
    assert change.delta == 0 and change.reasons == []


def test_per_turn_and_per_session_caps():
    big = _estimate(valence=-1.0, anger=1.0)
    assert rel.update(0.0, big, SETTINGS, new_day=False).delta >= -0.5
    # セッションの合計が上限に達していれば、それ以上は動かない
    assert rel.update(0.0, big, SETTINGS, session_delta=-2.0).delta == 0
    assert rel.update(0.0, big, SETTINGS, session_delta=-1.8).delta == pytest.approx(-0.2)
    assert rel.update(0.0, _estimate(valence=1, joy=1, trust=1), SETTINGS, session_delta=2.0).delta == 0


def test_value_is_clipped_and_low_confidence_moves_less():
    assert rel.update(19.9, _estimate(valence=1, joy=1, trust=1), SETTINGS).value_after == 20.0
    strong = rel.update(0.0, _estimate(valence=1, joy=1, trust=1, confidence=1.0), SETTINGS).delta
    weak = rel.update(0.0, _estimate(valence=1, joy=1, trust=1, confidence=0.3), SETTINGS).delta
    assert 0 < weak < strong


def test_degraded_estimate_does_not_move_relationship():
    assert rel.update(0.0, _estimate(valence=-1, anger=1.0), SETTINGS, degraded=True).delta == 0


def test_new_day_adds_a_little():
    change = rel.update(0.0, _estimate(target="situation"), SETTINGS, new_day=True)
    assert change.delta == pytest.approx(0.1)


# --- ユーザーの session_mood ---


def test_session_mood_moves_gradually():
    first = update_session_mood(None, EmotionVector.from_estimate(_estimate(valence=-1.0, sadness=1.0)))
    assert first.vad["valence"] == -1.0
    second = update_session_mood(first, EmotionVector.from_estimate(_estimate(valence=1.0, joy=1.0)))
    assert -1.0 < second.vad["valence"] <= -0.7  # 1ターンでは大きく動かない
    assert 0 < second.categories["joy"] <= 0.3


# --- キャラクタ感情(§7.1) ---

TEMPERAMENT = Temperament.from_json({"baseline_vad": {"v": 0.2, "a": 0.0, "d": 0.0}, "reactivity": 0.5, "recovery_rate": 0.0})


def test_sync_follows_relationship_monotonically():
    user = EmotionVector.from_estimate(_estimate(valence=-1.0, sadness=1.0))
    results = []
    for r in (-20, -10, 0, 10, 20):
        updated = update_companion(EmotionVector(), TEMPERAMENT, user, rel.sync_weight(r, SETTINGS), [])
        results.append(updated.state.categories["sadness"])
    assert results[:3] == [0.0, 0.0, 0.0]  # r ≤ 0 では同期しない
    assert results[3] == pytest.approx(0.25) and results[4] == pytest.approx(0.5)


def test_low_confidence_weakens_sync():
    user = EmotionVector.from_estimate(_estimate(sadness=1.0))
    w = rel.sync_weight(20, SETTINGS)
    full = update_companion(EmotionVector(), TEMPERAMENT, user, w * 1.0, []).state.categories["sadness"]
    half = update_companion(EmotionVector(), TEMPERAMENT, user, w * 0.4, []).state.categories["sadness"]
    assert half < full


def test_no_emotion_mapping_only_same_coordinates():
    # ユーザーの怒りを、キャラクタの別の感情(恐怖など)に変換しない
    user = EmotionVector.from_estimate(_estimate(anger=1.0))
    state = update_companion(EmotionVector(), TEMPERAMENT, user, 0.5, []).state
    assert state.categories["anger"] > 0
    assert all(v == 0 for k, v in state.categories.items() if k != "anger")


def test_recovery_returns_to_baseline():
    temperament = Temperament.from_json({"baseline_vad": {"v": 0.2}, "reactivity": 0.5, "recovery_rate": 0.5})
    state = EmotionVector()
    state.categories["fear"] = 0.8
    for _ in range(12):
        state = update_companion(state, temperament, None, 0.0, []).state
    assert state.categories["fear"] < 0.01
    assert state.vad["valence"] == pytest.approx(0.2, abs=0.01)


def test_topic_rules_accumulate_and_recover_after_topic_change():
    # ホラーの話題が3ターン続くと恐怖が累積し、話題が変わると回復する(§14)
    rules = [{"topic": "ホラー・オカルト", "effects": {"fear": 1.0}, "gain": 0.7}]
    temperament = Temperament.from_json({"reactivity": 0.6, "recovery_rate": 0.3})
    state, previous, levels = EmotionVector(), set(), []
    for _ in range(3):
        firings = fire_rules(rules, {0: 0.9}, temperament.reactivity, previous)
        state = update_companion(state, temperament, None, 0.0, firings).state
        previous = {f.index for f in firings}
        levels.append(state.categories["fear"])
    assert levels[0] < levels[1] < levels[2] < 1.0  # 累積するが、1回では飽和しない
    after = update_companion(state, temperament, None, 0.0, []).state.categories["fear"]
    assert after < levels[2]


def test_rule_threshold_and_repeat_damping():
    rules = [{"topic": "りんご", "effects": {"joy": 1.0}, "gain": 0.5}]
    assert fire_rules(rules, {0: 0.49}, 1.0) == []  # 閾値未満なら発火しない
    assert fire_rules(rules, {5: 0.9}, 1.0) == []  # 範囲外の番号は無視する
    first = fire_rules(rules, {0: 1.0}, 1.0)[0].scale
    repeated = fire_rules(rules, {0: 1.0}, 1.0, {0})[0].scale
    assert repeated == pytest.approx(first * 0.5)  # 続けて出ても、毎回満額にしない


# --- 応答の長さ(§8.2) ---


@pytest.mark.parametrize("t, name", [(0.0, "short"), (0.24, "short"), (0.25, "medium"), (0.69, "medium"), (0.7, "long"), (1.0, "long")])
def test_length_for(t, name):
    assert length_for(t)[0] == name


# --- エンジンでの状態更新 ---


def _persona(character_id: str = "sample_kuchibeta") -> Persona:
    row = pio.to_character_row(lint(pio.load_raw(pio.persona_path(character_id))).definition)
    return Persona(
        character_id=row["character_id"],
        name=row["name"],
        persona_prompt=row["persona_prompt"],
        speech_style=row["speech_style"],
        initial_relationship=row["initial_relationship"],
        axes=row["base_personality_json"]["axes"],
        traits=row["traits_json"],
        temperament=row["temperament_json"],
        appraisal_rules=row["appraisal_rules_json"],
    )


@pytest.fixture
def env():
    stores = InMemoryStores()
    stores.add_character(_persona("sample_kuchibeta"))
    stores.add_character(_persona("sample_hitonatsukoi"))
    llm = MockLLMClient()
    return Engine(llm, stores, SETTINGS), stores, llm


def _turn(text: str, character_id: str = "sample_kuchibeta") -> TurnInput:
    return TurnInput(channel="cli", character_id=character_id, text=text)


async def test_relationship_starts_from_character_initial_value(env):
    engine, stores, _ = env
    await engine.handle_turn(USER_A, _turn("こんにちは"), NOW)
    await engine.handle_turn(USER_A, _turn("こんにちは", "sample_hitonatsukoi"), NOW)
    assert stores.relationships[(USER_A, "sample_kuchibeta")] == 0.0
    assert stores.relationships[(USER_A, "sample_hitonatsukoi")] == 6.0  # 人懐こいは +6 から


async def test_gratitude_to_character_raises_relationship_and_is_recorded(env):
    engine, stores, _ = env
    result = await engine.handle_turn(USER_A, _turn("あなたと話せて嬉しい。ありがとう"), NOW)
    assert result.state["relationship"]["delta"] > 0
    event = stores.relationship_events[(USER_A, "sample_kuchibeta")][0]
    assert event["reason_code"] == "affection_to_character" and event["turn_no"] == 1
    turn = stores.turns[(USER_A, "sample_kuchibeta")][0]
    assert turn.relationship_value == 0.0 and turn.talkativeness_eff == 0.3


async def test_session_cap_holds_over_many_turns(env):
    engine, stores, _ = env
    for minute in range(12):
        await engine.handle_turn(USER_A, _turn("お前は最悪だ。ふざけるな"), NOW + timedelta(minutes=minute))
    assert stores.relationships[(USER_A, "sample_kuchibeta")] == pytest.approx(-2.0)


async def test_state_is_separated_by_user_and_character(env):
    engine, stores, _ = env
    await engine.handle_turn(USER_A, _turn("あなたと話せて嬉しい。ありがとう"), NOW)
    await engine.handle_turn(USER_B, _turn("こんにちは"), NOW)
    await engine.handle_turn(USER_A, _turn("こんにちは", "sample_hitonatsukoi"), NOW)
    assert stores.relationships[(USER_A, "sample_kuchibeta")] > 0
    assert stores.relationships[(USER_B, "sample_kuchibeta")] == 0.0
    # 別のキャラクタの user_state・companion_state には影響しない(§4.2)
    assert stores.user_mood[(USER_A, "sample_hitonatsukoi")][1].categories["joy"] == 0.0
    assert stores.user_mood[(USER_A, "sample_kuchibeta")][1].categories["joy"] > 0


async def test_companion_syncs_only_with_positive_relationship(env):
    engine, stores, _ = env
    await stores.set_relationship(USER_A, "sample_kuchibeta", 20.0, "test", NOW)
    await stores.set_relationship(USER_B, "sample_kuchibeta", -10.0, "test", NOW)
    close = await engine.handle_turn(USER_A, _turn("今日は悲しいことがあって落ち込んでる"), NOW)
    distant = await engine.handle_turn(USER_B, _turn("今日は悲しいことがあって落ち込んでる"), NOW)
    assert close.state["companion"]["categories"]["sadness"] > 0.2
    assert distant.state["companion"]["categories"]["sadness"] == 0.0
    assert close.state["sync_weight"] > 0 and distant.state["sync_weight"] == 0


async def test_topic_rule_moves_companion_emotion(env):
    engine, _, _ = env
    result = await engine.handle_turn(USER_A, _turn("今度、人前で話すことになってさ"), NOW)
    companion = result.state["companion"]
    assert companion["categories"]["fear"] > 0
    # 「人前で話す」は、好き嫌いシートの嫌いなこと(fear)。文脈によらず反応する
    assert [h["catalog_id"] for h in companion["sheet"] if "delta" in h] == ["action.public_speaking"]
    assert companion["sheet_effects"]["fear"] > 0


async def test_draft_gets_persona_context_but_no_emotion(env):
    engine, stores, llm = env
    await stores.set_relationship(USER_A, "sample_kuchibeta", 20.0, "test", NOW)
    await engine.handle_turn(USER_A, _turn("今日は悲しいことがあって落ち込んでる"), NOW)
    context = llm.last_draft_context
    assert "繊細" in context.style_card  # 夢見役(iaef)の文体カード
    assert "言いよどみ" in context.articulation  # 口下手(articulation 0.2)
    assert "大切に思っている" in context.relationship  # 親愛の帯の記述
    assert "やや長め" in context.length  # 口数の実効値 0.72 → long
    # 感情の情報(推定結果、数値)は、下書きに渡さない(§8.1)
    joined = context.style_card + context.relationship + context.articulation + context.length
    assert "sadness" not in joined and "悲し" not in joined and "20" not in joined


async def test_new_day_bonus_only_on_a_later_day(env):
    engine, stores, _ = env
    key = (USER_A, "sample_kuchibeta")
    await engine.handle_turn(USER_A, _turn("こんにちは"), NOW)
    await engine.close_session(USER_A, "sample_kuchibeta")
    await engine.handle_turn(USER_A, _turn("また来たよ"), NOW + timedelta(hours=1))  # 同じ日
    assert stores.relationships[key] == 0.0
    await engine.close_session(USER_A, "sample_kuchibeta")
    await engine.handle_turn(USER_A, _turn("おはよう"), NOW + timedelta(days=1))  # 翌日
    assert stores.relationships[key] == pytest.approx(0.1)
    await engine.handle_turn(USER_A, _turn("今日はね"), NOW + timedelta(days=1, minutes=1))  # 同じセッション
    assert stores.relationships[key] == pytest.approx(0.1)


async def test_degraded_estimate_does_not_change_state(env):
    engine, stores, llm = env
    await stores.set_relationship(USER_A, "sample_kuchibeta", 10.0, "test", NOW)
    llm.fail_emotion = True
    result = await engine.handle_turn(USER_A, _turn("お前は最悪だ"), NOW)
    assert result.state["relationship"]["delta"] == 0 and result.state["sync_weight"] == 0


async def test_relationship_view_and_debug_set(env):
    engine, _, _ = env
    view = await engine.relationship(USER_A, "sample_hitonatsukoi")
    assert view["band"] == "友好" and view["value"] == 6.0
    updated = await engine.set_relationship(USER_A, "sample_hitonatsukoi", -12)
    assert updated["band"] == "敵対・嫌悪"
    with pytest.raises(Exception, match="-20"):
        await engine.set_relationship(USER_A, "sample_hitonatsukoi", 25)
