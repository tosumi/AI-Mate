"""応答への評価(人間評価)のテスト(本体仕様書 §14、§15.1 の M6-7)。

評価は、作り方の文脈(キャラクタの版、応答モード、作り方、生成のモデル、プロンプトの版)を、
本人のターンの記録から写して持つ。会話の本文は持たない。集計は、件数と平均だけ。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mycompanion.account.service import AccountError, AccountOps
from mycompanion.account.store import InMemoryAccountStore
from mycompanion.channel.types import TurnInput
from mycompanion.cli.main import _rating_payload
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.eval.__main__ import render_ratings
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.memory.store import InMemoryMemoryStore

USER, OTHER = uuid.uuid4(), uuid.uuid4()
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def env():
    stores, mstore = InMemoryStores(), InMemoryMemoryStore()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    engine = Engine(MockLLMClient(), stores, EngineSettings(), None)
    store = InMemoryAccountStore(stores, mstore)
    return engine, stores, store, AccountOps(store)


async def _turn(engine, user=USER, text="今日は疲れた", compose=None, minutes=1):
    turn = TurnInput(channel="cli", character_id="char_1", text=text)
    return await engine.handle_turn(user, turn, NOW + timedelta(minutes=minutes), compose_mode=compose)


async def test_turn_record_keeps_how_the_reply_was_made(env):
    engine, _, _, _ = env
    result = await _turn(engine)
    context = await engine.turn_context(USER, "char_1", result.session_id, result.turn_no)
    assert context["compose_mode"] == "single" and context["response_mode"] in ("full", "brief")
    assert context["reply_model"] and context["reply_prompt_version"]  # 実際のモデルでは draft-v5+single-v1
    two = await _turn(engine, compose="two_stage", minutes=2)
    context = await engine.turn_context(USER, "char_1", two.session_id, two.turn_no)
    assert context["compose_mode"] == "two_stage" and "filter" in context["reply_model"]
    # ほかのユーザーのターン、存在しないターンは引けない
    assert await engine.turn_context(OTHER, "char_1", result.session_id, result.turn_no) is None
    assert await engine.turn_context(USER, "char_1", result.session_id, 99) is None


async def test_rate_validates_records_overwrites_exports_and_deletes(env):
    engine, stores, store, ops = env
    await stores.set_relationship(USER, "char_1", 1.0, "test", NOW)
    result = await _turn(engine)
    context = await engine.turn_context(USER, "char_1", result.session_id, result.turn_no)
    rate = lambda scores, note="", ctx=context: ops.rate(
        USER, "char_1", result.session_id, result.turn_no, scores, note, ctx
    )
    with pytest.raises(AccountError) as error:
        await rate({"overall": 6})
    assert error.value.code == "invalid_value"
    with pytest.raises(AccountError):
        await rate({"overall": None})  # 総合は必須
    with pytest.raises(AccountError) as error:
        await rate({"overall": 3}, ctx=None)  # 本人のターンが見つからない
    assert error.value.code == "turn_not_found"

    first = await rate({"overall": 2, "persona_fit": 4}, "  少し説教くさい  ")
    again = await rate({"overall": 4, "persona_fit": 5, "naturalness": 4, "empathy": 3}, "付け直し")
    assert first["id"] == again["id"] and len(store.ratings) == 1  # 同じターンは上書き
    (rating,) = store.ratings
    assert (rating["overall"], rating["empathy"], rating["note"], rating["compose_mode"]) == (4, 3, "付け直し", "single")
    # 会話の本文は持たない
    assert not any(k in rating for k in ("user_input", "reply_text", "text"))

    exported = (await ops.export(USER))["characters"]["char_1"]["ratings"]
    assert [r["overall"] for r in exported] == [4] and all("user_id" not in r for r in exported)
    await ops.delete(USER)
    assert store.ratings == []


async def test_summary_groups_by_how_the_reply_was_made_and_returns_no_ids_or_notes(env):
    engine, _, _, ops = env
    for minutes, (user, compose, score) in enumerate(
        [(USER, None, 4), (USER, None, 5), (OTHER, None, 2), (USER, "two_stage", 3)], start=1
    ):
        result = await _turn(engine, user=user, compose=compose, minutes=minutes, text=f"話その{minutes}")
        context = await engine.turn_context(user, "char_1", result.session_id, result.turn_no)
        await ops.rate(user, "char_1", result.session_id, result.turn_no, {"overall": score}, "メモ", context)
    rows = await ops.rating_summary(NOW - timedelta(days=1))
    by_mode = {r["compose_mode"]: r for r in rows}
    single, two = by_mode["single"], by_mode["two_stage"]
    assert (single["n"], single["raters"], single["overall"], single["low"]) == (3, 2, 3.67, 1)
    assert (two["n"], two["overall"]) == (1, 3.0)
    text = repr(rows)
    assert str(USER) not in text and str(OTHER) not in text and "メモ" not in text and "話その" not in text
    # 件数が少ない組は、平均を出さない
    table = render_ratings(rows, min_n=2)
    assert "3.67" in table[2] + table[3] and any("| 1 | — |" in line for line in table)


def test_cli_rate_command_parsing():
    last = (str(uuid.uuid4()), 3)
    payload = _rating_payload("/rate 4 5 3 4 少し長い", "char_1", last)
    assert payload is not None
    assert (payload["overall"], payload["persona_fit"], payload["naturalness"], payload["empathy"]) == (4, 5, 3, 4)
    assert payload["note"] == "少し長い" and payload["turn_no"] == 3
    short = _rating_payload("/rate 2 説教くさい", "char_1", last)
    assert short is not None and short["overall"] == 2 and "persona_fit" not in short and short["note"] == "説教くさい"
    assert _rating_payload("/rate", "char_1", last) is None
    assert _rating_payload("/rate 9", "char_1", last) is None
    assert _rating_payload("/rate よかった", "char_1", last) is None


def test_api_rates_only_the_callers_own_turn():
    from fastapi.testclient import TestClient

    from mycompanion.api.app import AppDeps, create_app
    from tests.test_simulate import AUTH, FakeUsers, FakeVerifier

    stores, mstore = InMemoryStores(), InMemoryMemoryStore()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    store = InMemoryAccountStore(stores, mstore)
    deps = AppDeps(
        verifier=FakeVerifier(), users=FakeUsers(), engine=Engine(MockLLMClient(), stores, EngineSettings()),
        characters=stores, env_name="dev", account=AccountOps(store),
    )  # fmt: skip
    client = TestClient(create_app(deps))
    turn = client.post("/v1/turns", json={"channel": "cli", "character_id": "char_1", "text": "ただいま"}, headers=AUTH)
    data = turn.json()
    body = {"character_id": "char_1", "session_id": data["session_id"], "turn_no": data["turn_no"], "overall": 5, "empathy": 4}
    done = client.post("/v1/ratings", json=body, headers=AUTH)
    assert done.status_code == 200 and done.json()["rated"]
    assert store.ratings[0]["compose_mode"] == "single"
    # 存在しないターン・範囲外の点は、受け付けない
    assert client.post("/v1/ratings", json=body | {"turn_no": 99}, headers=AUTH).status_code == 404
    assert client.post("/v1/ratings", json=body | {"overall": 0}, headers=AUTH).status_code == 422


async def test_saved_summary_survives_rater_deletion_and_compares_versions(env):
    # 評価者がアカウントを削除すると、その人の評価は消える。保存した集計は変わらず、版ごとに比べられる
    engine, stores, _, ops = env
    for minutes, (user, score) in enumerate([(USER, 5), (USER, 4), (OTHER, 2)], start=1):
        result = await _turn(engine, user=user, minutes=minutes, text=f"話その{minutes}")
        context = await engine.turn_context(user, "char_1", result.session_id, result.turn_no)
        await ops.rate(user, "char_1", result.session_id, result.turn_no, {"overall": score}, "メモ", context)
    since = NOW - timedelta(days=1)
    with pytest.raises(AccountError):
        await ops.save_rating_snapshot("  ", since)
    await ops.save_rating_snapshot("v1 の期間", since, min_n=3)
    small = await ops.save_rating_snapshot("少数", since, min_n=10)
    assert small["saved"]

    await stores.set_relationship(OTHER, "char_1", 1.0, "test", NOW)
    await ops.delete(OTHER)
    assert (await ops.rating_summary(since))[0]["n"] == 2  # いまの集計からは、消えた評価が抜ける
    v1, few = await ops.list_rating_snapshots()
    (row,) = v1["rows"]
    assert (v1["label"], row["n"], row["raters"], row["overall"], row["low"]) == ("v1 の期間", 3, 2, 3.67, 1)
    # 件数が min_n 未満の組は、平均と低い評価の件数を保存しない(個人の点が推測されないように)
    assert few["rows"][0]["overall"] is None and few["rows"][0]["low"] is None and few["rows"][0]["n"] == 3
    # 保存した集計には、ID・本文・メモが入らない
    text = repr(v1)
    assert str(USER) not in text and str(OTHER) not in text and "メモ" not in text and "話その" not in text
    from mycompanion.eval.__main__ import render_snapshots

    table = render_snapshots([v1, few])
    assert "v1 の期間" in table[2] and "3.67" in table[2] and "| — |" in table[3]
