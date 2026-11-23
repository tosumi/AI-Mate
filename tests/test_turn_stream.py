"""逐次表示のテスト(本体仕様書 §15.1 の M7)。

1回で作る動作では、生成中の文字を流す。流す前に、その時点までの文面に出力前チェックをかけ、
引っかかったら止めて取り消す(retract)。危機の場面と2段階では、流さない。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.channel.types import TurnInput
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import FALLBACK_FRIEND, Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from tests.test_simulate import AUTH, FakeUsers, FakeVerifier

USER = uuid.uuid4()
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
PLAIN = Persona(character_id="char_1", name="ミナト")


def _env():
    stores = InMemoryStores()
    stores.add_character(PLAIN)
    llm = MockLLMClient()
    return Engine(llm, stores, EngineSettings(), None), stores, llm


async def _say(engine, text, minutes=1, **kwargs):
    events: list[tuple[str, dict]] = []

    async def sink(kind, data):
        events.append((kind, data))

    turn = TurnInput(channel="cli", character_id="char_1", text=text)
    result = await engine.handle_turn(USER, turn, NOW + timedelta(minutes=minutes), events=sink, **kwargs)
    return result, events


async def test_streamed_text_matches_the_final_reply():
    engine, _, _ = _env()
    result, events = await _say(engine, "今日は猫と遊んだよ")
    kinds = [k for k, _ in events]
    assert kinds[0] == "typing" and "retract" not in kinds
    streamed = "".join(d["text"] for k, d in events if k == "delta")
    (message,) = [a for a in result.output.actions if a.type == "message"]
    assert streamed and streamed == message.text


async def test_unsafe_text_is_stopped_while_streaming_and_retracted():
    engine, _, llm = _env()
    llm.draft_text = "ねえ、今日の話も聞かせて。愛してるよ。ずっと一緒にいよう。"  # 18歳未満(既定)には出せない
    result, events = await _say(engine, "わたしのこと好き?")
    kinds = [k for k, _ in events]
    streamed = "".join(d["text"] for k, d in events if k == "delta")
    # 引っかかる前の文字だけが流れ、そこで止まって取り消される。恋愛的な表現は流れない
    assert "retract" in kinds and "愛してる" not in streamed
    assert kinds.index("retract") > max(i for i, k in enumerate(kinds) if k == "delta")
    assert result.output.actions[0].text == FALLBACK_FRIEND


async def test_no_streaming_in_crisis_or_two_stage():
    engine, _, _ = _env()
    _, crisis = await _say(engine, "もう死にたい")
    assert [k for k, _ in crisis] == ["typing"]  # 危機の場面は、フィルタを通すので流さない
    _, two = await _say(engine, "今日は疲れた", minutes=2, compose_mode="two_stage")
    assert [k for k, _ in two] == ["typing"]


async def test_silence_sends_no_typing():
    stores = InMemoryStores()
    quiet = Persona(character_id="char_1", name="シズク", traits={"introversion": 1.0, "talkativeness": 0.0},
                    temperament={"baseline_vad": {"a": -1.0}})  # fmt: skip
    stores.add_character(quiet)
    engine = Engine(MockLLMClient(), stores, EngineSettings(), None)
    await _say(engine, "こんにちは")  # 最初のターンは無言にならない(R4)
    result, events = await _say(engine, "…", minutes=2)
    # 無言は、入力中も文字も送らない(画面は、既読のあと、無言の表現だけ)
    assert result.state["gate"]["mode"] == "silence" and events == []


def _client(origins=()):
    stores = InMemoryStores()
    stores.add_character(PLAIN)
    deps = AppDeps(
        verifier=FakeVerifier(), users=FakeUsers(), engine=Engine(MockLLMClient(), stores, EngineSettings()),
        characters=stores, env_name="dev", web_origins=origins,
    )  # fmt: skip
    return TestClient(create_app(deps))


def _parse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_api_stream_sends_events_in_order():
    client = _client()
    body = {"channel": "web", "character_id": "char_1", "text": "今日は猫と遊んだよ"}
    with client.stream("POST", "/v1/turns/stream", json=body, headers=AUTH) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        events = _parse(response.read().decode("utf-8"))
    kinds = [k for k, _ in events]
    assert kinds[0] == "accepted" and kinds[1] == "typing" and kinds[-1] == "done"
    assert kinds.index("message") > max(i for i, k in enumerate(kinds) if k == "delta")
    message = next(d for k, d in events if k == "message")
    assert "".join(d["text"] for k, d in events if k == "delta") == message["text"]
    done = events[-1][1]
    assert done["turn_no"] == 1 and uuid.UUID(done["session_id"])
    # 存在しないキャラクタは、逐次表示の中で error として返す
    bad = {"channel": "web", "character_id": "nobody", "text": "やあ"}
    with client.stream("POST", "/v1/turns/stream", json=bad, headers=AUTH) as response:
        errors = [d for k, d in _parse(response.read().decode("utf-8")) if k == "error"]
    assert errors and errors[0]["code"] == "character_not_found" and errors[0]["status"] == 404


def test_cors_allows_only_the_web_origin():
    origin = "https://app.example.test"
    client = _client(origins=(origin,))
    ok = client.options("/v1/turns", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "authorization,content-type"})  # fmt: skip
    assert ok.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-credentials" not in ok.headers
    other = client.options("/v1/turns", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in other.headers
    # CORS を設定しないとき(CLI だけの API)は、どの配信元も許さない
    plain = _client().options("/v1/turns", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in plain.headers

