"""1回で応答を作る動作(既定。本体仕様書 §8.1、§15.1 の M6-6)のテスト。

感情推定と Planner のあとに、応答方針を指示に含めて、1回の呼び出しで応答を作る。感情フィルタは通さない
(危機フローだけは、安全のため通す)。出力前チェック・年齢の制約・安全の案内は、2段階と同じに働くこと。
"""

import uuid
from datetime import UTC, datetime, timedelta

from mycompanion.channel.types import MessageAction, TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import FALLBACK_FRIEND, Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.llm.prompts.draft import AGE_CONSTRAINT

USER = uuid.uuid4()
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
PLAIN = Persona(character_id="char_1", name="ミナト", persona_prompt="口下手で、言葉を選ぶのに時間がかかる。")


def _env():
    stores = InMemoryStores()
    stores.add_character(PLAIN)
    llm = MockLLMClient()
    return Engine(llm, stores, EngineSettings()), stores, llm


async def _say(engine, text, minutes=1, user=USER, **kwargs):
    turn = TurnInput(channel="cli", character_id="char_1", text=text)
    return await engine.handle_turn(user, turn, NOW + timedelta(minutes=minutes), **kwargs)


def test_single_is_the_default():
    assert EngineSettings().compose_mode == "single"


async def test_one_call_with_the_directive_and_no_filter():
    engine, stores, llm = _env()
    result = await _say(engine, "同僚に手柄を横取りされた。本当に腹が立つ。")
    (action,) = result.output.actions
    # 方針つきの1回だけで、フィルタは呼ばない。応答は最終の応答(整えた応答)として返す
    assert llm.draft_calls == 1 and llm.filter_calls == 0
    assert isinstance(action, MessageAction) and action.variant == "filtered"
    assert result.output.debug_marker == "🎭"  # 整えた応答の印(下書きの印ではない)
    directive = llm.last_draft_context.directive
    assert "<directive>" in directive and "戦略:" in directive and "<user_state>" in directive
    record = stores.turns[(USER, "char_1")][0]
    assert record.filtered_text == record.draft_text and record.edit_summary is None
    # filter_mode=both でも、比べる下書きはないので、応答は1つ
    both = await _say(engine, "ただいま", minutes=2, filter_mode="both")
    assert len([a for a in both.output.actions if a.type == "message"]) == 1


async def test_age_constraint_and_output_check_still_apply():
    engine, stores, llm = _env()
    await _say(engine, "こんにちは")  # 既定は、18歳未満フラグ true(安全側)
    assert AGE_CONSTRAINT in llm.last_draft_context.constraints
    llm.draft_text = "愛してるよ。ずっと一緒にいよう。"
    result = await _say(engine, "わたしのこと好き?", minutes=2)
    # 出力前チェックで止め、1回だけ作り直し、だめなら友情の枠の定型応答(2段階と同じ)
    assert result.output.actions[0].text == FALLBACK_FRIEND and llm.filter_calls == 0
    assert any(e["category"] == "romantic_to_minor" for e in stores.security_events)


async def test_crisis_still_goes_through_the_filter():
    engine, _, llm = _env()
    result = await _say(engine, "もう死にたい")
    action, notice = result.output.actions
    # 危機フローでは、1回の応答のあとに、安全のため感情フィルタも通す。相談窓口の案内はコードで付ける
    assert "重要: 相手の安全に関わる場面" in llm.last_draft_context.directive
    assert llm.filter_calls == 1 and llm.last_filter_context.crisis is True
    assert action.variant == "filtered" and notice.kind == "crisis_resources"


async def test_two_stage_can_be_chosen_per_turn_for_verification():
    engine, _, llm = _env()
    await _say(engine, "今日は疲れた", compose_mode="two_stage")
    # 2段階: 下書きには方針を入れず(感情の情報なし)、感情フィルタで整える
    assert llm.last_draft_context.directive == "" and llm.filter_calls == 1


def test_api_accepts_the_compose_parameter():
    from tests.test_simulate import AUTH, _client

    client = _client(settings=EngineSettings())
    body = {"channel": "cli", "character_id": "char_1", "text": "今日は疲れた"}
    single = client.post("/v1/turns", json=body, params={"filter": "both"}, headers=AUTH).json()
    assert [a["variant"] for a in single["output"]["actions"]] == ["filtered"]
    two = client.post("/v1/turns", json=body, params={"compose": "two_stage", "filter": "both"}, headers=AUTH).json()
    assert [a["variant"] for a in two["output"]["actions"]] == ["draft", "filtered"]
    assert client.post("/v1/turns", json=body, params={"compose": "nope"}, headers=AUTH).status_code == 422
