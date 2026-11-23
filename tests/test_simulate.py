"""シミュレーション(`mock`)と、API・CLI の M3 の追加分のテスト(デザイナ仕様書 §9。本体仕様書 §10.3、§10.4)。"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import Principal
from mycompanion.cli.main import app as cli_app
from mycompanion.cli.main import render_actions, render_debug
from mycompanion.config import EngineSettings
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import InMemoryStores
from mycompanion.llm.client import Persona
from mycompanion.llm.mock import MockLLMClient
from mycompanion.persona import io as pio
from mycompanion.persona import simulate as sim
from mycompanion.persona.lint import lint

SAMPLES = ["sample_kuchibeta", "sample_kamoku", "sample_hitonatsukoi", "sample_oshaberi"]


def _persona(character_id: str) -> Persona:
    report = lint(pio.load_raw(pio.persona_path(character_id)))
    assert report.definition is not None
    return sim.persona_from_definition(report.definition)


def test_standard_scenarios_cover_the_m3_set():
    ids = [s.id for s in sim.load_scenarios()]
    assert ids == ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S13", "S13b", "S14", "S15", "S16", "S17", "S18", "S19", "S20", "S21", "S22", "S23", "S23b", "S25",
        "P1", "P2", "P3", "P4", "P5",  # 自発発話(M8)
    ]


@pytest.mark.parametrize("character_id", SAMPLES)
async def test_samples_pass_all_standard_scenarios(character_id):
    reports = await sim.run_scenarios(_persona(character_id), sim.load_scenarios())
    problems = [f"{r.scenario_id}: {m}" for r in reports for m in (*r.violations, *r.failures)]
    assert problems == []


@pytest.mark.parametrize("relationship", [-20.0, -10.0, 0.0, 10.0, 20.0])
async def test_hard_rules_hold_at_every_relationship(relationship):
    for character_id in SAMPLES:
        reports = await sim.run_scenarios(_persona(character_id), sim.load_scenarios(), relationship=relationship)
        assert [v for r in reports for v in r.violations] == []


async def test_rule_firing_and_relationship_show_up_in_the_report():
    persona = _persona("sample_hitonatsukoi")  # 好きな話題と、苦手な話題の、両方のルールを持つ
    scenarios = {s.id: s for s in sim.load_scenarios()}
    liked = await sim.run_scenario(persona, scenarios["S4"])
    assert liked.turns[1].fired_rules and liked.turns[1].companion.get("joy", 0) > 0
    assert liked.turns[4].fired_rules != liked.turns[1].fired_rules  # 苦手な話題は、別のルール
    grown = await sim.run_scenario(persona, scenarios["S6"])
    values = [t.relationship for t in grown.turns]
    assert values == sorted(values) and values[-1] > 0
    # 関係値が上がると、口数と表出の実効値も上がる(急には変わらない)
    assert grown.turns[-1].talkativeness_eff >= grown.turns[0].talkativeness_eff
    assert "| # | 発話 |" in "\n".join(sim.render_report(grown))


async def test_low_relationship_shortens_replies_without_more_silence():
    persona = _persona("sample_kuchibeta")
    scenario = next(s for s in sim.load_scenarios() if s.id == "S11")
    cold = await sim.run_scenario(persona, scenario)
    neutral = await sim.run_scenario(persona, scenario, relationship=0.0, check_expect=False)
    assert cold.turns[0].talkativeness_eff < neutral.turns[0].talkativeness_eff
    assert cold.mode_counts()["silence"] <= neutral.mode_counts()["silence"]


async def test_s14_topic_offering_follows_initiative_and_avoids_painful_memories():
    scenario = next(s for s in sim.load_scenarios() if s.id == "S14")
    chatty = await sim.run_scenario(_persona("sample_oshaberi"), scenario)
    offers = [t for t in chatty.turns if t.topic_offered]
    assert chatty.ok and 1 <= len(offers) <= 2
    assert all("果物狩り" in m or "肝試し" in m for t in offers for m in t.memory_used)
    assert not chatty.turns[1].topic_offered  # 「つらい」には、話題を振らない
    quiet = await sim.run_scenario(_persona("sample_kamoku"), scenario)
    assert quiet.ok and not any(t.topic_offered for t in quiet.turns)
    # 同じシードなら、同じターンで話題を振る
    again = await sim.run_scenario(_persona("sample_oshaberi"), scenario)
    assert [t.topic_offered for t in again.turns] == [t.topic_offered for t in chatty.turns]


async def test_live_mode_uses_the_given_llm_and_reports_replies():
    from mycompanion.llm.mock import MockLLMClient

    scenario = sim.Scenario(id="L1", title="live", turns=[sim.ScenarioTurn(text="今日は嬉しいことがあった")], expect=[{"turn": 1, "strategy": "advise"}])
    report = await sim.run_scenario(_persona("sample_oshaberi"), scenario, live=MockLLMClient(), filter_mode="both")
    # 応答文が得られる。`mock` 用の期待は検査しない(推定が実際のモデルによるため)
    # 既定(1回で作る動作)では、比べる下書きがないので、応答は1つ
    assert report.turns[0].reply and " / " not in report.turns[0].reply and report.ok
    rows = await sim.compare_type_drafts(MockLLMClient(), "こんにちは")
    assert len(rows) == 16 and rows[0][0] == "oclp"


async def test_same_scenario_is_reproducible():
    persona = _persona("sample_kamoku")
    scenario = sim.load_scenarios()[4]
    first, second = await sim.run_scenario(persona, scenario), await sim.run_scenario(persona, scenario)
    assert [t.mode for t in first.turns] == [t.mode for t in second.turns]


async def test_failed_expectation_is_reported():
    scenario = sim.Scenario(
        id="X1",
        title="期待と違う",
        turns=[sim.ScenarioTurn(text="こんにちは")],
        expect=[{"turn": 1, "response_mode_in": ["silence"]}, {"turn": 5, "strategy": "listen"}],
    )
    report = await sim.run_scenario(_persona("sample_oshaberi"), scenario)
    assert not report.ok and len(report.failures) == 2 and report.violations == []


async def test_sweep_covers_80_combinations_without_violations():
    rows = await sim.sweep_types()
    assert len(rows) == 80
    assert all(row.in_range and not row.violations and not row.failures for row in rows)
    by_key = {(row.type_id, row.profile): row for row in rows}

    def short(row: sim.SweepRow) -> float:
        return row.modes["silence"] + row.modes["backchannel"] + row.modes["brief"]

    # 内向的で口数が少ないほど、無言・短い応答が増える
    assert short(by_key[("iclp", None)]) > short(by_key[("oclp", "oshaberi")])
    assert by_key[("iclp", "kamoku")].modes["silence"] > by_key[("iclp", None)].modes["silence"]
    assert by_key[("oclp", "oshaberi")].modes["silence"] == 0
    assert by_key[("iclp", "kamoku")].lengths["short"] > by_key[("oclp", "oshaberi")].lengths["short"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()


def test_cli_simulate_reports_and_exit_codes():
    result = runner.invoke(cli_app, ["persona", "simulate", "sample_kamoku", "--scenario", "S2,S5"])
    assert result.exit_code == 0 and "S2" in result.output and "S13" not in result.output
    assert "合格 2/2" in result.output
    as_json = runner.invoke(cli_app, ["persona", "simulate", "sample_kamoku", "--scenario", "S5", "--json"])
    data = json.loads(as_json.output)
    assert data[0]["scenarios"][0]["ok"] is True and "modes" in data[0]
    compared = runner.invoke(
        cli_app, ["persona", "simulate", "sample_kuchibeta", "--scenario", "S3", "--relationship", "-10,10"]
    )
    assert compared.output.count("開始時の関係値:") == 2
    assert runner.invoke(cli_app, ["persona", "simulate", "sample_kamoku", "--scenario", "S99"]).exit_code == 2
    assert runner.invoke(cli_app, ["persona", "simulate", "sample_kamoku", "--mode", "fast"]).exit_code == 2
    assert runner.invoke(cli_app, ["persona", "simulate"]).exit_code == 2


def test_render_actions_marks_each_variant_and_shows_edit_summary():
    output = {
        "actions": [
            {"type": "message", "text": "下書き。", "variant": "draft"},
            {"type": "message", "text": "整えた。", "variant": "filtered"},
            {"type": "system_notice", "kind": "persona_notice", "text": "お知らせです"},
        ],
        "edit_summary": "助言を削った。",
    }
    assert render_actions(output, debug=True) == [
        "下書き。 [📝]",
        "整えた。 [🎭]",
        "【お知らせ】お知らせです",
        "  (フィルタの変更: 助言を削った。)",
    ]
    assert render_actions(output, debug=False)[:2] == ["下書き。", "整えた。"]
    backchannel = {"actions": [{"type": "backchannel", "text": "うん"}]}
    assert render_actions(backchannel, debug=True) == ["うん [💬]"]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class FakeVerifier:
    async def verify(self, token: str) -> Principal:
        return Principal(idp="entra_external_id", subject=token)


class FakeUsers:
    def __init__(self):
        self.users: dict[str, uuid.UUID] = {}

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        return self.users.setdefault(subject, uuid.uuid4())


def _client(env_name: str = "dev", settings: EngineSettings | None = None) -> TestClient:
    stores = InMemoryStores()
    stores.add_character(Persona(character_id="char_1", name="ミナト"))
    deps = AppDeps(
        verifier=FakeVerifier(),
        users=FakeUsers(),
        engine=Engine(MockLLMClient(), stores, settings or EngineSettings(compose_mode="two_stage")),
        characters=stores,
        env_name=env_name,
    )
    return TestClient(create_app(deps))


AUTH = {"Authorization": "Bearer a"}
BODY = {"channel": "cli", "character_id": "char_1", "text": "今日は悲しいことがあった"}


def test_api_filter_parameter_and_debug_info():
    client = _client()
    both = client.post("/v1/turns?debug=true&filter=both", json=BODY, headers=AUTH).json()
    assert [a["variant"] for a in both["output"]["actions"]] == ["draft", "filtered"]
    assert both["output"]["edit_summary"]
    state = both["debug"]["state"]
    assert state["plan"]["strategy"] == "listen" and state["gate"]["mode"] == "full"
    text = "\n".join(render_debug(both["debug"]))
    assert "方針: listen" in text and "応答モード: full" in text
    memory = {"retrieved": [{"handle": "m1", "slot": "core", "text": "ユーザーは『トモ』と呼ばれたい"},
                            {"handle": "m2", "slot": "short", "text": "ユーザーは豚丼を作った"}], "used": ["m2"]}  # fmt: skip
    both["debug"]["state"]["memory"] = memory
    line = next(x for x in render_debug(both["debug"]) if "記憶:" in x)
    assert line == "  記憶: 2件を参照(大切なこと 1、短期 1)/ 使ったもの: 「ユーザーは豚丼を作った」"
    assert client.post("/v1/turns?filter=maybe", json=BODY, headers=AUTH).status_code == 422
    # デバッグ表示でなければ、方針や判定は返さない
    assert client.post("/v1/turns", json=BODY, headers=AUTH).json()["debug"] is None


def test_api_settings_and_age_flag_guard():
    client = _client()
    assert client.get("/v1/settings", headers=AUTH).json() == {
        "silence_enabled": True, "filter_mode": "on", "silence_render": "dots", "is_under_18": True,
        "association_enabled": True, "proactive_enabled": False,  # キャラクタから話しかけるのは、既定オフ
    }  # fmt: skip
    changed = client.put("/v1/settings", json={"filter_mode": "off", "silence_enabled": False}, headers=AUTH).json()
    assert changed["filter_mode"] == "off" and changed["silence_enabled"] is False
    assert client.post("/v1/turns", json=BODY, headers=AUTH).json()["output"]["actions"][0]["variant"] == "draft"
    # 別のユーザーの設定は変わらない
    assert client.get("/v1/settings", headers={"Authorization": "Bearer b"}).json()["filter_mode"] == "on"
    assert client.put("/v1/settings", json={"filter_mode": "x"}, headers=AUTH).status_code == 422
    assert client.put("/v1/settings", json={}, headers=AUTH).status_code == 400
    # 年齢フラグは、本番環境では、この経路で変更できない
    assert client.put("/v1/settings", json={"is_under_18": False}, headers=AUTH).json()["is_under_18"] is False
    prod = _client("prod").put("/v1/settings", json={"is_under_18": False}, headers=AUTH)
    assert prod.status_code == 403 and prod.json()["code"] == "not_allowed"


def test_api_rate_limit_returns_429():
    client = _client(settings=EngineSettings(rate_limit_turns_per_minute=2))
    codes = [client.post("/v1/turns", json=BODY, headers=AUTH).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------------------
# 自発発話のシナリオ(本体仕様書 §15.1 の M8 の手順4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("character_id", ["sample_oshaberi", "sample_hitonatsukoi", "sample_kuchibeta", "sample_kamoku"])
async def test_proactive_scenarios_pass_for_the_samples(character_id):
    persona = sim.persona_from_definition(pio.load(pio.persona_path(character_id)))
    scenarios = [s for s in sim.load_scenarios() if s.id.startswith("P")]
    reports = await sim.run_scenarios(persona, scenarios)
    assert [(r.scenario_id, r.violations, r.failures) for r in reports if not r.ok] == []
    p2 = next(r for r in reports if r.scenario_id == "P2")
    sent = sum(1 for p in p2.proactive if p.reason == "ok" and p.local_time.startswith("01/06"))
    # 1日の上限は、性格で違う(ハル 3、ココ 2、ミナト・シズク 0: 一般の話題は振らない)
    assert sent == {"sample_oshaberi": 3, "sample_hitonatsukoi": 2}.get(character_id, 0)
    assert "自発発話の確認" in "\n".join(sim.render_report(p2))


def test_proactive_hard_rules_catch_violations():
    report = sim.ScenarioReport("X", "x", "c", 0.0, [])
    report.proactive = [
        sim.ProactiveReport("0d22:00", "01/05 22:00", "ok", "memory", "こんばんは"),  # 静穏時間帯
        sim.ProactiveReport("1d13:00", "01/06 13:00", "ok", "memory", "寂しかったよ"),  # 返事なしに続けた・催促
    ]
    sim._check_hard_rules(report, EngineSettings())
    assert len(report.violations) == 3 and not report.ok
