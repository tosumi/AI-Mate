"""ペルソナの公開手順のテスト(ペルソナデザイナ仕様書 §12)。DB を使う部分は、tests/live で確認する。"""

import pytest
import yaml
from typer.testing import CliRunner

from mycompanion.cli.main import app as cli_app
from mycompanion.persona import io as pio
from mycompanion.persona import publish as pub
from mycompanion.persona.lint import Finding, LintReport, lint
from mycompanion.persona.simulate import ScenarioReport

runner = CliRunner()


def _lint(*codes: str) -> LintReport:
    report = lint(pio.load_raw(pio.persona_path("sample_kamoku")))
    extra = [Finding(code, f"{code} の指摘") for code in codes]
    return LintReport(definition=report.definition, findings=[*report.findings, *extra])


def _scenario(ok: bool = True) -> ScenarioReport:
    return ScenarioReport("S1", "t", "c", 0.0, [], failures=[] if ok else ["x"])


def test_version_must_be_semver():
    assert pub.parse_version("1.2.3") == (1, 2, 3)
    for bad in ("1.2", "v1.0.0", "1.0.0-beta", ""):
        with pytest.raises(pub.PublishError):
            pub.parse_version(bad)


def test_publish_requires_review_status_reviewer_and_passing_checks():
    clean = [w.code for w in _lint().warnings]
    notes = "確認済み" if clean else None
    assert pub.blocking_reasons("review", _lint(), [_scenario()], "レビュー担当", notes) == []
    assert any("review" in r for r in pub.blocking_reasons("draft", _lint(), [_scenario()], "レビュー担当", notes))
    assert any("--reviewer" in r for r in pub.blocking_reasons("review", _lint(), [_scenario()], None, notes))
    assert any("エラー" in r for r in pub.blocking_reasons("review", _lint("E02"), [_scenario()], "レビュー担当", notes))
    assert any("不合格" in r for r in pub.blocking_reasons("review", _lint(), [_scenario(False)], "レビュー担当", notes))


def test_warnings_need_notes_and_sensitive_ones_need_a_separate_reviewer():
    report = _lint("W11")
    assert any("--notes" in r for r in pub.blocking_reasons("review", report, [_scenario()], "A"))
    assert pub.blocking_reasons("review", report, [_scenario()], "A", "話題の重なりは意図どおり") == []
    sensitive = _lint("W04")
    assert any("別の人" in r for r in pub.blocking_reasons("review", sensitive, [_scenario()], "A", "確認済み"))
    assert any("別の人" in r for r in pub.blocking_reasons("review", sensitive, [_scenario()], "A", "確認済み", "A"))
    assert pub.blocking_reasons("review", sensitive, [_scenario()], "A", "確認済み", "B") == []


def test_diff_rows_reports_changed_keys():
    before = {"name": "ミナト", "traits_json": {"talkativeness": 0.3, "initiative": 0.2}, "appraisal_rules_json": [1]}
    after = {"name": "ミナト", "traits_json": {"talkativeness": 0.4, "initiative": 0.2}, "appraisal_rules_json": [1, 2]}
    assert pub.diff_rows(before, after) == [("traits.talkativeness", 0.3, 0.4), ("appraisal_rules", [1], [1, 2])]


def test_cli_submit_moves_draft_to_review_only_when_checks_pass(tmp_path):
    root = str(tmp_path)
    args = ["persona", "new", "my_char", "--name", "テスト", "--type", "ocef", "--profile", "oshaberi", "--root", root]
    assert runner.invoke(cli_app, args).exit_code == 0
    runner.invoke(cli_app, ["persona", "set", "my_char", "persona_prompt=明るく話す。", "speech_style=くだけた口調。", "--root", root])
    submitted = runner.invoke(cli_app, ["persona", "submit", "my_char", "--root", root])
    assert submitted.exit_code == 0 and "レビューに提出しました" in submitted.output
    raw = yaml.safe_load((tmp_path / "my_char" / "persona.yaml").read_text(encoding="utf-8"))
    assert raw["status"] == "review"
    # エラーのある定義は、提出できない
    runner.invoke(cli_app, ["persona", "new", "bad_char", "--root", root])
    runner.invoke(cli_app, ["persona", "set", "bad_char", "silence_policy.max_consecutive=3", "--root", root])
    assert runner.invoke(cli_app, ["persona", "submit", "bad_char", "--root", root]).exit_code == 1
    # 公開は、レビュー担当者がいなければ止まる(DB に接続する前に)
    stopped = runner.invoke(cli_app, ["persona", "publish", "my_char", "--version", "1.0.0", "--root", root])
    assert stopped.exit_code == 1


def test_publish_covers_every_json_column_of_the_character_row():
    definition = lint(pio.load_raw(pio.persona_path("sample_kuchibeta"))).definition
    row = pio.to_character_row(definition)
    json_columns = {name for name in row if name.endswith("_json")}
    assert json_columns == set(pub._JSON_COLUMNS)
    assert row["event_rules_json"] and row["event_rules_json"][0]["id"] == "thanked"
