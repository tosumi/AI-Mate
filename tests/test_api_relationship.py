"""関係値の API と、ペルソナの CLI のテスト(本体仕様書 §10.3。デザイナ仕様書 §13)。"""

import uuid

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import Principal
from mycompanion.cli.main import app as cli_app
from mycompanion.cli.main import render_debug, render_relationship
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.persona import quiz


class FakeVerifier:
    async def verify(self, token: str) -> Principal:
        return Principal(idp="entra_external_id", subject=token)


class FakeUsers:
    def __init__(self):
        self.users: dict[str, uuid.UUID] = {}

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return self.users.setdefault(subject, uuid.uuid4())


def _client(env_name: str = "dev") -> TestClient:
    stores = InMemoryStores()
    stores.add_character(Persona(character_id="char_1", name="ミナト", initial_relationship=5.0))
    deps = AppDeps(
        verifier=FakeVerifier(),
        users=FakeUsers(),
        engine=Engine(MockLLMClient(), stores, EngineSettings()),
        characters=stores,
        env_name=env_name,
    )
    return TestClient(create_app(deps))


AUTH = {"Authorization": "Bearer a"}


def test_relationship_hides_number_unless_debug():
    client = _client()
    plain = client.get("/v1/relationship?character_id=char_1", headers=AUTH).json()
    assert plain["band"] == "友好" and "value" not in plain
    debug = client.get("/v1/relationship?character_id=char_1&debug=true", headers=AUTH).json()
    assert debug["value"] == 5.0
    assert render_relationship(plain)[0] == "関係: 友好" and len(render_relationship(debug)) == 3


def test_set_relationship_validates_and_is_per_user():
    client = _client()
    body = {"character_id": "char_1", "value": -12}
    assert client.put("/v1/relationship", json=body, headers=AUTH).json()["band"] == "敵対・嫌悪"
    other = client.get("/v1/relationship?character_id=char_1&debug=true", headers={"Authorization": "Bearer b"})
    assert other.json()["value"] == 5.0  # 別のユーザーには影響しない
    bad = client.put("/v1/relationship", json={"character_id": "char_1", "value": 30}, headers=AUTH)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_value"


def test_set_relationship_is_blocked_in_production():
    response = _client("prod").put("/v1/relationship", json={"character_id": "char_1", "value": 1}, headers=AUTH)
    assert response.status_code == 403 and response.json()["code"] == "not_allowed"


def test_debug_output_includes_state_summary():
    client = _client()
    body = {"channel": "cli", "character_id": "char_1", "text": "あなたと話せて嬉しい。ありがとう"}
    info = client.post("/v1/turns?debug=true", json=body, headers=AUTH).json()["debug"]
    assert info["state"]["relationship"]["delta"] > 0
    text = "\n".join(render_debug(info))
    assert "関係値" in text and "キャラクタの感情" in text


# --- ペルソナの CLI ---

runner = CliRunner()


def _run(*args: str):
    return runner.invoke(cli_app, ["persona", *args])


def test_persona_cli_new_set_validate_show(tmp_path):
    root = str(tmp_path)
    assert _run("new", "my_char", "--name", "テスト", "--type", "ocef", "--profile", "oshaberi", "--root", root).exit_code == 0
    assert _run("new", "my_char", "--root", root).exit_code == 1  # すでに存在する
    result = _run("set", "my_char", "persona_prompt=明るく話す。", "speech_style=くだけた口調。", "--root", root)
    assert result.exit_code == 0
    assert _run("validate", "my_char", "--root", root).exit_code == 0
    shown = _run("show", "my_char", "--derived", "--relationship", "-20,0,20", "--root", root)
    assert shown.exit_code == 0 and "talkativeness" in shown.output and "盛り上げ役" not in shown.output
    assert "関係値" in shown.output

    # 絶対ルール R1 に反する値は、エラーで終了する
    bad = _run("set", "my_char", "silence_policy.max_consecutive=3", "--root", root)
    assert "E02" in bad.output
    assert _run("validate", "my_char", "--root", root).exit_code == 1


def test_persona_cli_type_commands(tmp_path):
    root = str(tmp_path)
    listed = _run("type", "list", "--axis", "i=high,e=high")
    assert listed.exit_code == 0 and "iaef" in listed.output and "oclp" not in listed.output
    assert "夢見役" in _run("type", "show", "iaef").output
    assert _run("type", "show", "zzzz").exit_code == 2
    assert _run("type", "compare", "iaef", "oclp").exit_code == 0

    _run("new", "c2", "--root", root)
    assert "堅実役" in _run("type", "set", "c2", "iclp", "--root", root).output
    raw = yaml.safe_load((tmp_path / "c2" / "persona.yaml").read_text(encoding="utf-8"))
    assert raw["base_personality"]["type"] == "iclp" and raw["base_personality"]["axes"]["i"] == 0.8


@pytest.mark.parametrize("adopt, expected_i", [("preset", 0.2), ("scores", 0.1)])
def test_persona_cli_quiz_from_answers_file(tmp_path, adopt, expected_i):
    root = str(tmp_path)
    _run("new", "c3", "--root", root)
    answers = tmp_path / "answers.yaml"
    answers.write_text(yaml.safe_dump({"answers": quiz.answers_for_type("oaef")}), encoding="utf-8")
    result = _run("type", "quiz", "c3", "--answers", str(answers), "--adopt", adopt, "--root", root)
    assert result.exit_code == 0 and "ひらめき役" in result.output and "診断ではありません" in result.output
    base = yaml.safe_load((tmp_path / "c3" / "persona.yaml").read_text(encoding="utf-8"))["base_personality"]
    assert base["type"] == "oaef" and base["source"] == "quiz" and base["axes"]["i"] == expected_i
    assert base["quiz"]["adopt"] == adopt and len(base["quiz"]["answers"]) == 24
