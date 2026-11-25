"""ウィザード(本体仕様書 §15.1 の M10-2): 質問に答えるだけで、ペルソナ定義ができあがる。

状態を持たない。回答の矛盾は、確認の質問を挟む。組み立てた定義は、CLI と同じ lint を通る。
"""

import random
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import AuthError, Principal
from mycompanion.persona import io as pio
from mycompanion.persona import quiz, wizard
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import lint
from mycompanion.persona.resolve import resolve
from mycompanion.persona.schema import PersonaDefinition
from mycompanion.state import catalog

AUTH = {"Authorization": "Bearer user-token"}
CHAR = "my_char"
NAME = "ミオ"


def _answer_all(answers: dict, pick=lambda q: q["default"]) -> dict:
    """残りの質問に答えていき、組み立てた結果を返す(pick: 質問 → 回答)。"""
    while True:
        out = wizard.step(CHAR, NAME, answers)
        if out["done"]:
            return out | {"answers": answers}
        question = out["question"]
        answers[question["id"]] = pick(question)


def _type_answers(type_id: str) -> dict[str, int]:
    main = {q.id for q in wizard.TYPE_MAIN}
    return {k: v for k, v in quiz.answers_for_type(type_id).items() if k in main}


def _codes(definition: dict) -> set[str]:
    return {f.code for f in lint(definition).findings}


def test_answering_every_question_builds_a_valid_definition():
    out = _answer_all({})
    report = lint(out["definition"])
    assert report.ok
    assert out["definition"]["character_id"] == CHAR and out["definition"]["name"] == NAME
    # 「どちらともいえない」ばかりなら、各軸に予備の質問と、近いほうの極の選択が入る
    asked = set(out["answers"])
    assert {q.id for qs in wizard.TYPE_TIEBREAK.values() for q in qs} <= asked
    assert {q.id for q in wizard.POLE.values()} <= asked
    assert out["progress"]["answered"] == out["progress"]["total"] == len(asked)


@pytest.mark.parametrize("type_id", [t.type_id for t in ptypes.all_types()])
def test_each_type_comes_from_four_questions_per_axis(type_id):
    out = _answer_all(_type_answers(type_id))
    base = out["definition"]["base_personality"]
    assert base["type"] == type_id and base["source"] == "quiz"
    assert ptypes.type_id_from_axes(base["axes"]) == type_id
    assert not any(q.id in out["answers"] for q in wizard.POLE.values())  # はっきり答えれば、追加の質問はない


def test_introverted_but_talkative_asks_to_confirm_and_can_reduce():
    # やや内向的(内向度 0.75。「やや当てはまる」の向き)で、とてもよくしゃべる
    leaning = {q.id: (2 if quiz.QUESTIONS_BY_ID[q.id].reverse else 4) for q in wizard.TYPE_MAIN if q.id.startswith("i")}
    answers = _type_answers("iaef") | leaning | {"ex_talk": 5}
    while (out := wizard.step(CHAR, NAME, answers))["question"] and out["question"]["id"] != "fu_w01":
        answers[out["question"]["id"]] = out["question"]["default"]
    question = out["question"]
    assert question["id"] == "fu_w01" and question["note"]
    assert "W01" in _codes(_answer_all(dict(answers, fu_w01="keep"))["definition"])
    reduced = _answer_all(dict(answers, fu_w01="reduce"))
    assert "W01" not in _codes(reduced["definition"]) and "W01" in reduced["acknowledged"]


def test_expressive_but_silent_and_sticky_feelings_ask_to_confirm():
    answers = _type_answers("iaef") | {"ex_talk": 1, "ex_express": 5, "tm_reactivity": 5, "tm_recovery": 1}
    out = _answer_all(answers, pick=lambda q: {"fu_w10": "express_less", "fu_w07": "recover"}.get(q["id"], q["default"]))
    assert {"fu_w10", "fu_w07"} <= set(out["answers"])
    assert not {"W10", "W07"} & _codes(out["definition"])
    assert out["definition"]["temperament"]["recovery_rate"] >= 0.2


def test_no_likes_asks_for_one_and_uses_it():
    def pick(q):
        if q["id"] == "fu_w08":
            return {"food": "food.onigiri"}
        return q["default"]

    out = _answer_all(_type_answers("ocef"), pick)
    assert "fu_w08" in out["answers"]
    assert out["definition"]["likes_dislikes"]["liked"]["food"]["id"] == "food.onigiri"
    assert "W08" not in _codes(out["definition"])


def test_disliked_items_ask_how_the_character_reacts():
    answers = _type_answers("icep") | {"lk_liked": {"color": "color.green"}, "lk_disliked": {"food": "food.natto", "action": "action.dark"}}
    out = _answer_all(answers, pick=lambda q: "fear" if q["id"] == "lk_dislike_reaction" else q["default"])
    disliked = out["definition"]["likes_dislikes"]["disliked"]
    assert disliked["food"]["emotions"] == ["fear"] and disliked["action"]["emotion"] == "fear"
    assert lint(out["definition"]).ok


def test_finish_fills_the_rest_with_default_answers():
    out = wizard.step(CHAR, NAME, {"co_role": "listener"}, finish=True)
    assert out["done"] and out["defaulted"] > 30
    definition = out["definition"]
    assert definition["base_personality"]["type"] == wizard.DEFAULT_TYPE  # 性格の質問に答えていなければ、既定のタイプ
    assert definition["concept"]["role"] == "listener"
    assert lint(definition).ok


def test_persona_text_comes_from_the_type_style_card_and_answers():
    answers = _type_answers("ocef") | {"ex_talk": 5, "tm_mood": "bright", "sp_first_person": "boku", "sp_tone": "casual", "sp_ending": "ne"}
    definition = _answer_all(answers)["definition"]
    assert ptypes.get_type("ocef").style_card in definition["persona_prompt"]
    assert "おしゃべり" in definition["persona_prompt"] and "明るく元気" in definition["persona_prompt"]
    assert definition["speech_style"].startswith("一人称は「ぼく」。") and "タメ口" in definition["speech_style"]


def test_answers_move_expression_in_the_answered_direction():
    quiet = _answer_all(_type_answers("ocef") | {"ex_talk": 2})["definition"]
    chatty = _answer_all(_type_answers("ocef") | {"ex_talk": 4})["definition"]
    plain = _answer_all(_type_answers("ocef"))["definition"]
    talk = [resolve(PersonaDefinition.model_validate(d)).talkativeness for d in (quiet, plain, chatty)]
    assert talk[0] < talk[1] <= talk[2]


def test_random_answers_never_build_an_invalid_definition():
    rng = random.Random(1125)

    def pick(q):
        if q["kind"] == "scale":
            return rng.randint(1, 5)
        if q["kind"] == "choice":
            return rng.choice(q["options"])["value"]
        return {}

    for _ in range(60):
        out = _answer_all({}, pick)
        report = lint(out["definition"])
        assert report.ok
        unexpected = {f.code for f in report.findings if f.level == "warning"} - set(out["acknowledged"]) - {"W21", "W08", "W23"}
        assert not unexpected
        assert len(out["answers"]) <= 48


def test_definition_round_trips_through_cli_yaml():
    definition = _answer_all(_type_answers("iclp"))["definition"]
    again = yaml.safe_load(pio.dump(definition))  # CLI と同じ書式で保存して、読み直す
    assert lint(again).ok
    assert again["base_personality"] == definition["base_personality"] and again["event_rules"] == definition["event_rules"]


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"nope": 1}, "ウィザードにない質問"),
        ({"i1": 6}, "1〜5"),
        ({"co_role": "boss"}, "選択肢にない"),
        ({"lk_liked": {"food": "color.green"}}, "好みカタログにない"),
        ({"lk_liked": {"food": "food.onigiri"}, "lk_disliked": {"food": "food.onigiri"}}, "両方"),
    ],
)
def test_bad_answers_are_refused(answers, message):
    with pytest.raises(wizard.WizardError, match=message):
        wizard.step(CHAR, NAME, answers)


def test_only_dislikeable_items_can_be_disliked():
    only_liked = next(i for i in catalog.items() if not i.dislikeable)
    with pytest.raises(wizard.WizardError, match="苦手には選べません"):
        wizard.step(CHAR, NAME, {"lk_disliked": {only_liked.category: only_liked.id}})


# --- API ---


class FakeVerifier:
    async def verify(self, token: str) -> Principal:
        if token != "user-token":
            raise AuthError("bad")
        return Principal(idp="test", subject="designer-user")


class FakeUsers:
    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return uuid.UUID(int=1)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(AppDeps(verifier=FakeVerifier(), users=FakeUsers(), personas_root=Path("personas"))))


def test_wizard_api_asks_the_next_question_and_builds_with_check(client):
    body = {"character_id": CHAR, "name": NAME, "answers": {}}
    assert client.post("/v1/designer/wizard", json=body).status_code == 401
    first = client.post("/v1/designer/wizard", json=body, headers=AUTH).json()
    assert first["question"]["id"] == "co_role" and first["question"]["text"].startswith(NAME)
    assert first["done"] is False and "definition" not in first
    done = client.post("/v1/designer/wizard", json=body | {"finish": True}, headers=AUTH).json()
    assert done["done"] and done["check"]["ok"] and done["check"]["resolved"]["type_id"] == wizard.DEFAULT_TYPE


def test_wizard_api_refuses_sample_ids_and_bad_answers(client):
    sample = client.post("/v1/designer/wizard", json={"character_id": "sample_x", "name": NAME}, headers=AUTH)
    assert sample.status_code == 422
    bad = client.post("/v1/designer/wizard", json={"character_id": CHAR, "name": NAME, "answers": {"i1": 9}}, headers=AUTH)
    assert bad.status_code == 422 and "1〜5" in bad.json()["detail"]
