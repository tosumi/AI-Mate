"""会話の API と、CLI の表示のテスト(本体仕様書 §10)。"""

import uuid

import pytest
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import AuthError, Principal
from mycompanion.cli.main import render_actions, render_debug
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient


class FakeVerifier:
    async def verify(self, token: str) -> Principal:
        if not token.startswith("good-"):
            raise AuthError("invalid")
        return Principal(idp="entra_external_id", subject=token)


class FakeUsers:
    def __init__(self):
        self.users: dict[tuple[str, str], uuid.UUID] = {}

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return self.users.setdefault((idp, subject), uuid.uuid4())


@pytest.fixture
def env():
    stores = InMemoryStores()
    stores.add_character(Persona(character_id="char_1", name="ミナト", persona_prompt="秘密の人格プロンプト"))
    llm = MockLLMClient()
    deps = AppDeps(
        verifier=FakeVerifier(),
        users=FakeUsers(),
        engine=Engine(llm, stores, EngineSettings()),
        characters=stores,
    )
    return TestClient(create_app(deps)), stores, llm


def _auth(name: str = "a") -> dict[str, str]:
    return {"Authorization": f"Bearer good-{name}"}


def _body(text: str, character_id: str = "char_1") -> dict[str, str]:
    return {"channel": "cli", "character_id": character_id, "text": text}


def test_turn_requires_auth(env):
    client, _, _ = env
    assert client.post("/v1/turns", json=_body("やあ")).status_code == 401


def test_turn_returns_message_and_hides_debug_by_default(env):
    client, _, _ = env
    response = client.post("/v1/turns", json=_body("今日は楽しい"), headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert data["turn_no"] == 1 and data["debug"] is None
    assert data["output"]["actions"][0]["type"] == "message"


def test_debug_returns_own_estimate(env):
    client, _, _ = env
    data = client.post("/v1/turns?debug=true", json=_body("今日は楽しい"), headers=_auth()).json()
    assert data["debug"]["estimate"]["categories"]["joy"] > 0.5
    assert data["debug"]["degraded"] is False


def test_user_id_in_body_is_rejected(env):
    # user_id はクライアントが指定できない(§10.1)
    client, _, _ = env
    body = {**_body("やあ"), "user_id": str(uuid.uuid4())}
    assert client.post("/v1/turns", json=body, headers=_auth()).status_code == 422


def test_users_do_not_share_sessions(env):
    client, stores, _ = env
    a = client.post("/v1/turns", json=_body("Aです"), headers=_auth("a")).json()
    b = client.post("/v1/turns", json=_body("Bです"), headers=_auth("b")).json()
    assert a["session_id"] != b["session_id"]
    assert len(stores.turns) == 2


@pytest.mark.parametrize(
    "body, status, code",
    [
        (_body(" "), 400, "empty_input"),
        (_body("あ" * 2001), 413, "input_too_long"),
        (_body("やあ", "char_x"), 404, "character_not_found"),
    ],
)
def test_engine_errors_map_to_http(env, body, status, code):
    client, _, _ = env
    response = client.post("/v1/turns", json=body, headers=_auth())
    assert response.status_code == status and response.json()["code"] == code


def test_llm_failure_is_503_without_internal_details(env):
    client, _, llm = env
    llm.fail_draft = True
    response = client.post("/v1/turns", json=_body("やあ"), headers=_auth())
    assert response.status_code == 503
    assert "mock" not in response.text and "Traceback" not in response.text


def test_characters_list_does_not_expose_persona_prompt(env):
    client, _, _ = env
    response = client.get("/v1/characters", headers=_auth())
    assert response.json() == [{"character_id": "char_1", "name": "ミナト"}]
    assert "秘密" not in response.text


def test_close_session(env):
    client, _, _ = env
    client.post("/v1/turns", json=_body("やあ"), headers=_auth())
    close = {"character_id": "char_1"}
    assert client.post("/v1/sessions/close", json=close, headers=_auth()).json() == {"closed": True}
    assert client.post("/v1/sessions/close", json=close, headers=_auth()).json() == {"closed": False}


# --- CLI の表示(§10.2、§10.4) ---


def test_render_message_with_and_without_marker():
    output = {"actions": [{"type": "message", "text": "うん。", "variant": "draft"}], "debug_marker": "📝"}
    assert render_actions(output, debug=False) == ["うん。"]
    assert render_actions(output, debug=True) == ["うん。 [📝]"]


def test_render_silence_and_notice():
    output = {"actions": [{"type": "silence", "render": "dots"}], "debug_marker": "🤐"}
    assert render_actions(output, debug=True) == ["・・・ [🤐]"]
    assert render_actions(output, debug=False, silence_render="none") == []
    notice = {"actions": [{"type": "system_notice", "kind": "throttle_start", "text": "一時停止します"}]}
    assert render_actions(notice, debug=False) == ["【お知らせ】一時停止します"]


def test_render_debug_summary(env):
    client, _, _ = env
    info = client.post("/v1/turns?debug=true", json=_body("旅行の話、楽しみ"), headers=_auth()).json()["debug"]
    text = "\n".join(render_debug(info))
    assert "anticipation" in text and "旅行" in text


def test_conversation_returns_recent_turns_even_after_the_idle_timeout(env):
    # 画面を開いたときに、これまでの会話を表示する(本人の会話だけ。2026-11-23 から、終わった会話も返す)
    import asyncio
    from datetime import UTC, datetime, timedelta

    client, stores, _ = env
    client.post("/v1/turns", json=_body("今日は楽しい"), headers=_auth("a"))
    client.post("/v1/turns", json=_body("明日も晴れるといいな"), headers=_auth("a"))
    data = client.get("/v1/conversation?character_id=char_1", headers=_auth("a")).json()
    assert [t["user_input"] for t in data["turns"]] == ["今日は楽しい", "明日も晴れるといいな"]
    assert [t["turn_no"] for t in data["turns"]] == [1, 2] and data["session_id"]
    assert all(t["reply_text"] and t["session_id"] == data["session_id"] for t in data["turns"])
    assert client.get("/v1/conversation?character_id=char_1", headers=_auth("b")).json() == {
        "session_id": None, "turns": []
    }
    assert client.get("/v1/conversation?character_id=char_1").status_code == 401
    # 無入力の期限(30分)を過ぎても、ターンは返す(進行中の会話ではないので、session_id は None)
    (key,) = stores.turns
    engine = client.app.state.deps.engine
    later = max(t.ts for t in stores.turns[key]) + timedelta(minutes=31)
    old = asyncio.run(engine.current_conversation(key[0], "char_1", now=later))
    assert old is not None and old["session_id"] is None
    assert [t["user_input"] for t in old["turns"]] == ["今日は楽しい", "明日も晴れるといいな"]
    assert datetime.now(UTC) - later < timedelta(days=1)


def test_conversation_spans_sessions_and_keeps_only_the_latest_turns(env):
    # 終わった会話と新しい会話をまたいで、直近の件数だけ、古い順に返す。各ターンに、そのセッションを付ける
    client, _, _ = env
    client.post("/v1/turns", json=_body("前の会話"), headers=_auth("a"))
    client.post("/v1/sessions/close", json={"character_id": "char_1"}, headers=_auth("a"))
    client.post("/v1/turns", json=_body("新しい会話"), headers=_auth("a"))
    data = client.get("/v1/conversation?character_id=char_1", headers=_auth("a")).json()
    assert [t["user_input"] for t in data["turns"]] == ["前の会話", "新しい会話"]
    first, second = data["turns"]
    assert first["session_id"] != second["session_id"] and second["session_id"] == data["session_id"]
    client.app.state.deps.engine._settings = client.app.state.deps.engine._settings.model_copy(
        update={"conversation_restore_turns": 1}
    )
    data = client.get("/v1/conversation?character_id=char_1", headers=_auth("a")).json()
    assert [t["user_input"] for t in data["turns"]] == ["新しい会話"]
