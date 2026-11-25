"""ペルソナデザイナの CLI(ペルソナデザイナ仕様書 §13)。

範囲: `new` / `list` / `show` / `set` / `validate` / `simulate`(`mock` / `live`)と、性格タイプの `type list|show|compare|set|quiz`。
公開の手順は `submit` / `publish` / `rollback` / `deprecate` / `versions` / `diff`(DB には、管理者として接続する)。
好き嫌いシートは `likes`(`--try` で発話への反応を確認)、選択肢は `catalog list`。
`suggest`、`explain`、LLM による検査は、まだない。

ペルソナ定義は、ローカルの `personas/<character_id>/persona.yaml` に置く。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from mycompanion.config import EngineSettings
from mycompanion.llm.client import EmotionContext
from mycompanion.llm.mock import MockLLMClient
from mycompanion.persona import io as pio
from mycompanion.persona import publish as pub
from mycompanion.persona import quiz
from mycompanion.persona import sheet as sheet_module
from mycompanion.persona import simulate as sim
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import LintReport, lint
from mycompanion.persona.resolve import (
    PROFILE_NAMES,
    PROFILE_PRESETS,
    default_traits,
    effective_traits,
    merged_preferences,
    resolve,
)
from mycompanion.state import appraisal, catalog

app = typer.Typer(help="ペルソナ(キャラクタ)の設計", no_args_is_help=True)
type_app = typer.Typer(help="性格タイプ(16プリセット)", no_args_is_help=True)
app.add_typer(type_app, name="type")

RootOption = Annotated[Path | None, typer.Option("--root", help="ペルソナ定義の置き場所(既定: personas)")]
JsonOption = Annotated[bool, typer.Option("--json", help="機械可読の形式で出力する")]


def _fail(message: str, code: int = 1) -> typer.Exit:
    print(f"エラー: {message}", file=sys.stderr)
    return typer.Exit(code)


def _load(character_id: str, root: Path | None) -> tuple[Path, dict[str, Any]]:
    path = pio.persona_path(character_id, root)
    if not path.exists():
        raise _fail(f"ペルソナ定義が見つかりません: {path}")
    return path, pio.load_raw(path)


def _print_findings(report: LintReport) -> None:
    labels = {"error": "エラー", "warning": "警告", "info": "情報"}
    for finding in report.findings:
        where = f"({finding.path})" if finding.path else ""
        print(f"  [{finding.code}] {labels[finding.level]}: {finding.message} {where}".rstrip())
    errors, warnings = len(report.errors), len(report.warnings)
    print(f"エラー {errors}件、警告 {warnings}件。" + ("" if report.ok else " エラーがあるため、公開できません。"))


def _bar(value: float, width: int = 10) -> str:
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# 作成・一覧・表示・設定・検査
# ---------------------------------------------------------------------------


@app.command()
def new(
    character_id: str,
    name: Annotated[str | None, typer.Option("--name", help="キャラクタの名前")] = None,
    type_id: Annotated[str, typer.Option("--type", help="性格タイプの ID(例: iaef)")] = "iaef",
    profile: Annotated[str | None, typer.Option("--profile", help="表出プロファイル(kuchibeta など)")] = None,
    wizard: Annotated[bool, typer.Option("--wizard", help="対話で、性格タイプの決め方を選ぶ")] = False,
    root: RootOption = None,
) -> None:
    """新しいペルソナ定義を作る。"""
    path = pio.persona_path(character_id, root)
    if path.exists():
        raise _fail(f"すでに存在します: {path}")
    try:
        raw = pio.new_definition(character_id, name or character_id, type_id, profile)
    except ValueError as exc:
        raise _fail(str(exc), 2) from None
    pio.save_raw(path, raw)
    print(f"作成しました: {path}")
    if wizard:
        print("性格タイプの決め方を選んでください:")
        print("  1) プリセットから選ぶ(16タイプ)")
        print("  2) 質問に答えて決める(24問、5分程度)")
        choice = input("> ").strip()
        if choice == "2":
            _run_quiz(character_id, root, resume=False, answers_file=None, adopt=None)
        else:
            _print_type_table(ptypes.all_types())
            chosen = input("タイプの ID > ").strip()
            if not ptypes.is_type_id(chosen):
                raise _fail(f"16タイプの ID ではありません: {chosen}", 2)
            _set_type(path, raw, chosen)
    print("persona_prompt と speech_style を書いてから、`companion persona validate` で検査してください。")


@app.command("list")
def list_personas(root: RootOption = None, as_json: JsonOption = False) -> None:
    """ペルソナ定義の一覧。"""
    rows = []
    for character_id in pio.list_ids(root):
        raw = pio.load_raw(pio.persona_path(character_id, root))
        base = raw.get("base_personality") or {}
        rows.append(
            {
                "character_id": character_id,
                "name": raw.get("name"),
                "type": base.get("type"),
                "profile": (raw.get("profile") or {}).get("preset"),
                "status": raw.get("status"),
                "version": raw.get("version"),
            }
        )
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for row in rows:
        print(
            f"{row['character_id']:<24} {row['name'] or '':<8} {row['type'] or '-':<5} "
            f"{row['profile'] or '-':<13} {row['status']} {row['version']}"
        )


@app.command()
def show(
    character_id: str,
    derived: Annotated[bool, typer.Option("--derived", help="解決済みの値と、導出した嗜好を表示する")] = False,
    relationship: Annotated[
        str | None, typer.Option("--relationship", help="関係値ごとの実効値を表示する(例: -20,-10,0,10,20)")
    ] = None,
    root: RootOption = None,
    as_json: JsonOption = False,
) -> None:
    """ペルソナ定義の内容を表示する。"""
    _, raw = _load(character_id, root)
    if not derived and relationship is None:
        print(json.dumps(raw, ensure_ascii=False, indent=2, default=str) if as_json else pio.dump(raw))
        return
    report = lint(raw)
    if report.definition is None or not report.ok:
        _print_findings(report)
        raise _fail("定義にエラーがあるため、解決済みの値を計算できません。")
    definition = report.definition
    traits = resolve(definition)
    axes = definition.base_personality.axes
    defaults = default_traits(axes.i, axes.e).as_dict()
    result: dict[str, Any] = {
        "traits": traits.as_dict(),
        "defaults": defaults,
        "style_card": ptypes.style_card(axes.model_dump()),
        "preferences": merged_preferences(definition),
    }
    if relationship is not None:
        try:
            values = [float(v) for v in relationship.split(",") if v.strip()]
        except ValueError:
            raise _fail("--relationship は、数値をカンマ区切りで指定してください。", 2) from None
        result["curve"] = [
            {
                "relationship": r,
                "talkativeness_eff": round(effective_traits(traits, r, 0.6).talkativeness_eff, 2),
                "expressiveness_eff": round(effective_traits(traits, r, 0.6).expressiveness_eff, 2),
            }
            for r in values
        ]
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"{definition.name}({definition.character_id})  性格タイプ: {definition.base_personality.type}")
    print("解決済みの値(基準値 = 関係値0のとき)。括弧内は、性格タイプの既定値:")
    for key, value in result["traits"].items():
        print(f"  {key:<26} {value:.2f}  ({defaults[key]:.2f})")
    print(f"文体カード: {result['style_card']}")
    print("嗜好:")
    for item in result["preferences"]:
        mark = {"explicit": "", "sheet": "(好き嫌いシート)"}.get(item["source"], "(話題ルールから導出)")
        print(f"  {item['topic']}: {item['affinity']:+.2f} {mark}".rstrip())
    for point in result.get("curve", []):
        print(
            f"  関係値 {point['relationship']:+5.0f}: 口数 {point['talkativeness_eff']:.2f} "
            f"/ 表出 {point['expressiveness_eff']:.2f}"
        )


@app.command("set")
def set_values(
    character_id: str,
    assignments: Annotated[list[str], typer.Argument(help="キー=値(例: profile.deltas.talkativeness=-0.1)")],
    root: RootOption = None,
) -> None:
    """パラメータを設定する。設定のあと、検査を実行する。"""
    path, raw = _load(character_id, root)
    for assignment in assignments:
        if "=" not in assignment:
            raise _fail(f"キー=値 の形式で指定してください: {assignment}", 2)
        key, value = assignment.split("=", 1)
        try:
            pio.set_value(raw, key.strip(), pio.parse_value(value))
        except KeyError as exc:
            raise _fail(str(exc), 2) from None
    pio.save_raw(path, raw)
    print(f"保存しました: {path}")
    _print_findings(lint(raw))


@app.command()
def validate(character_id: str, root: RootOption = None, as_json: JsonOption = False) -> None:
    """整合性チェック(lint)を実行する。エラーがあると、終了コード 1 を返す。"""
    _, raw = _load(character_id, root)
    report = lint(raw)
    if as_json:
        print(
            json.dumps(
                {
                    "ok": report.ok,
                    "findings": [
                        {"code": f.code, "level": f.level, "message": f.message, "path": f.path}
                        for f in report.findings
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_findings(report)
    if not report.ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# 性格タイプ
# ---------------------------------------------------------------------------


def _print_type_table(items: list[ptypes.PersonalityType]) -> None:
    for item in items:
        d = default_traits(item.axes["i"], item.axes["e"])
        print(
            f"  {item.type_id}  {item.name:<6} {item.composition}  "
            f"口数 {d.talkativeness:.2f} / 表出 {d.expressiveness:.2f} / 自発性 {d.initiative:.2f}"
        )


def _set_type(path: Path, raw: dict[str, Any], type_id: str) -> None:
    raw["base_personality"] = {
        "type": type_id,
        "axes": ptypes.preset_axes(type_id),
        "source": "preset",
        "quiz": None,
    }
    pio.save_raw(path, raw)
    item = ptypes.get_type(type_id)
    print(f"性格タイプを設定しました: {type_id} {item.name}({item.composition})")


@type_app.command("list")
def type_list(
    axis: Annotated[str | None, typer.Option("--axis", help="軸で絞り込む(例: i=high,e=high)")] = None,
) -> None:
    """16タイプの一覧。"""
    items = ptypes.all_types()
    for condition in (axis or "").split(","):
        if not condition.strip():
            continue
        name, _, level = condition.partition("=")
        name, level = name.strip(), level.strip()
        if name not in ptypes.AXES or level not in ("high", "low"):
            raise _fail("--axis は、i=high,e=low のように指定してください(軸は i / a / e / f)。", 2)
        items = [t for t in items if (t.axes[name] > 0.5) == (level == "high")]
    _print_type_table(items)


@type_app.command("show")
def type_show(type_id: str) -> None:
    """タイプの詳細(軸、文体カード、既定値)。"""
    if not ptypes.is_type_id(type_id):
        raise _fail(f"16タイプの ID ではありません: {type_id}", 2)
    item = ptypes.get_type(type_id)
    print(f"{item.type_id} {item.name}({item.composition})")
    print(f"軸: {item.axes}")
    print(f"文体カード: {item.style_card}")
    for key, value in default_traits(item.axes["i"], item.axes["e"]).as_dict().items():
        print(f"  {key:<26} {value:.2f}")


@type_app.command("compare")
def type_compare(first: str, second: str) -> None:
    """2タイプの比較。"""
    for type_id in (first, second):
        if not ptypes.is_type_id(type_id):
            raise _fail(f"16タイプの ID ではありません: {type_id}", 2)
    a, b = ptypes.get_type(first), ptypes.get_type(second)
    da, db = (default_traits(t.axes["i"], t.axes["e"]).as_dict() for t in (a, b))
    print(f"{'':<26} {a.type_id} {a.name:<8} {b.type_id} {b.name}")
    for axis in ptypes.AXES:
        print(f"{ptypes.AXIS_NAMES[axis]:<24} {ptypes.pole_name(axis, a.axes[axis]):<13} {ptypes.pole_name(axis, b.axes[axis])}")
    for key in da:
        print(f"{key:<26} {da[key]:<13.2f} {db[key]:.2f}")
    print(f"\n{a.type_id}: {a.style_card}\n{b.type_id}: {b.style_card}")


@type_app.command("set")
def type_set(character_id: str, type_id: str, root: RootOption = None) -> None:
    """プリセットから性格タイプを設定する(方式A)。"""
    if not ptypes.is_type_id(type_id):
        raise _fail(f"16タイプの ID ではありません: {type_id}", 2)
    path, raw = _load(character_id, root)
    _set_type(path, raw, type_id)
    _print_findings(lint(raw))


def _ask_answers(session_path: Path, answers: dict[str, int]) -> dict[str, int] | None:
    """対話で回答を集める。中断したら None(回答は、セッションのファイルに保存する)。"""
    questions = quiz.main_questions()
    print(quiz.PREAMBLE)
    print("  1 まったく当てはまらない  2 あまり  3 どちらとも  4 やや  5 とても当てはまる")
    print("  (s: スキップ / b: 戻る / q: 保存して中断)")
    index = 0
    already = set(answers)  # 再開のとき、回答済みの質問は飛ばす
    while index < len(questions):
        question = questions[index]
        if question.id in already:
            index += 1
            continue
        position = sum(1 for q in questions[: index + 1] if q.axis == question.axis)
        print(f"\n[{ptypes.AXIS_NAMES[question.axis]} {position}/6] このキャラクタは、{question.text}")
        reply = input("> ").strip().lower()
        if reply == "q":
            session_path.write_text(yaml.safe_dump({"answers": answers}, allow_unicode=True), encoding="utf-8")
            print(f"中断しました。再開: --resume({session_path})")
            return None
        if reply == "b":
            index = max(0, index - 1)
            answers.pop(questions[index].id, None)
            already.discard(questions[index].id)
            continue
        if reply == "s":
            index += 1
            continue
        if reply in ("1", "2", "3", "4", "5"):
            answers[question.id] = int(reply)
            index += 1
        else:
            print("1〜5、s、b、q のいずれかを入力してください。")
    return answers


def _print_quiz_result(result: quiz.QuizResult) -> None:
    print("結果:")
    for axis in ptypes.AXES:
        r = result.axes[axis]
        if r.score is None:
            print(f"  {ptypes.AXIS_NAMES[axis]}  回答が足りません({r.answered}問。4問以上が必要)")
            continue
        pole = dict(ptypes.AXIS_POLES[axis]).get(r.pole or "", "未決定")
        note = "  ← 境界に近い" if (r.strength or 0) < 0.2 else ""
        print(f"  {ptypes.AXIS_NAMES[axis]}  {_bar(r.score)}  {pole}  (強さ {r.strength:.2f}){note}")
    if result.type_id:
        print(f"  → {result.type_id} {ptypes.get_type(result.type_id).name}")
    for warning in result.warnings:
        print(f"  注意: {warning}")
    print(f"  ※ {quiz.DISCLAIMER}")


def _run_quiz(
    character_id: str, root: Path | None, resume: bool, answers_file: Path | None, adopt: str | None
) -> None:
    path, raw = _load(character_id, root)
    session_path = path.parent / "quiz_session.yaml"
    interactive = answers_file is None
    tiebreak_choice: dict[str, str] = {}

    if answers_file is not None:
        loaded = yaml.safe_load(answers_file.read_text(encoding="utf-8")) or {}
        answers = dict(loaded.get("answers", loaded))
        tiebreak_choice = dict(loaded.get("tiebreak_choice", {})) if "answers" in loaded else {}
    else:
        saved = yaml.safe_load(session_path.read_text(encoding="utf-8")) if resume and session_path.exists() else {}
        collected = _ask_answers(session_path, dict((saved or {}).get("answers", {})))
        if collected is None:
            return
        answers = collected

    try:
        result = quiz.score(answers, tiebreak_choice)
    except ValueError as exc:
        raise _fail(str(exc), 2) from None

    # 境界の軸: 予備の質問で再採点し、それでも決まらなければ、設計者に極を選んでもらう
    if interactive:
        for axis in [a for a, r in result.axes.items() if r.borderline and not r.used_tiebreak]:
            for question in quiz.tiebreak_questions(axis):
                print(f"\n[{ptypes.AXIS_NAMES[axis]} 予備] このキャラクタは、{question.text}")
                reply = input("> ").strip()
                if reply in ("1", "2", "3", "4", "5"):
                    answers[question.id] = int(reply)
        result = quiz.score(answers, tiebreak_choice)
        for axis in [a for a, r in result.axes.items() if r.borderline and r.pole is None]:
            (low_letter, low_name), (high_letter, high_name) = ptypes.AXIS_POLES[axis]
            reply = input(f"{ptypes.AXIS_NAMES[axis]}は境界です。どちらにしますか? 1) {low_name}  2) {high_name} > ").strip()
            tiebreak_choice[axis] = high_letter if reply == "2" else low_letter
        result = quiz.score(answers, tiebreak_choice)

    _print_quiz_result(result)
    if result.type_id is None:
        raise _fail(f"タイプを決められませんでした(未決定の軸: {', '.join(result.undecided_axes)})。")

    if adopt is None and interactive:
        print("採用方法: 1) このタイプのプリセット値  2) 回答のスコアをそのまま  3) 採用しない")
        reply = input("> ").strip()
        if reply == "3":
            return
        adopt = "scores" if reply == "2" else "preset"
    adopt = adopt or "preset"
    completed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    raw["base_personality"] = quiz.base_personality(result, answers, tiebreak_choice, adopt, completed_at)
    pio.save_raw(path, raw)
    session_path.unlink(missing_ok=True)
    print(f"性格タイプを設定しました({adopt}): {path}")
    _print_findings(lint(raw))


@type_app.command("quiz")
def type_quiz(
    character_id: str,
    resume: Annotated[bool, typer.Option("--resume", help="中断した診断を再開する")] = False,
    answers: Annotated[Path | None, typer.Option("--answers", help="回答のファイル(非対話)")] = None,
    adopt: Annotated[str | None, typer.Option("--adopt", help="採用方法: preset / scores")] = None,
    root: RootOption = None,
) -> None:
    """質問による診断で、性格タイプを決める(方式B)。想定するキャラクタについて答える。"""
    if adopt not in (None, "preset", "scores"):
        raise _fail("--adopt は preset か scores を指定してください。", 2)
    _run_quiz(character_id, root, resume, answers, adopt)


def _run_live(job: Any) -> Any:
    """実際の LLM(Azure OpenAI)に接続して、`job(llm)` を実行する。状態と記憶は、メモリ上に置く。"""
    from mycompanion import config as cfg
    from mycompanion.llm.azure_openai import AzureOpenAILLMClient

    async def run() -> Any:
        try:
            llm = AzureOpenAILLMClient(cfg.AzureOpenAIConfig.from_env(), cfg.EngineSettings())
        except cfg.ConfigError as exc:
            raise _fail(str(exc), 2) from None
        try:
            return await job(llm)
        finally:
            await llm.close()

    return asyncio.run(run())


@app.command()
def simulate(
    character_id: Annotated[str | None, typer.Argument(help="キャラクタのID(--sweep のときは不要)")] = None,
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="実行するシナリオ(例: S1,S2)。省略時は、すべて")
    ] = None,
    mode: Annotated[
        str, typer.Option("--mode", help="mock(LLM を呼ばない)/ live(実際の LLM。費用が発生する)")
    ] = "mock",
    filter_mode: Annotated[
        str | None, typer.Option("--filter", help="live のときの感情フィルタ: on / off / both")
    ] = None,
    compose: Annotated[
        str | None,
        typer.Option("--compose", help="応答の作り方: single(既定)/ two_stage(下書き → 感情フィルタ。検証用)"),
    ] = None,
    text: Annotated[
        str, typer.Option("--input", help="--sweep types --mode live で、16タイプに与える入力")
    ] = "最近、仕事がうまくいかなくて。",
    relationship: Annotated[
        str | None, typer.Option("--relationship", help="開始時の関係値を変えて比べる(例: -10,0,10,20)")
    ] = None,
    under_18: Annotated[bool, typer.Option("--under-18", help="18歳未満フラグが true のユーザーとして実行する")] = False,
    sweep: Annotated[str | None, typer.Option("--sweep", help="types: 16タイプ × 表出プロファイルの全数確認")] = None,
    root: RootOption = None,
    as_json: JsonOption = False,
) -> None:
    """標準シナリオで、ペルソナの挙動を確認する(R1〜R3 の違反がないことを必ず検査する)。"""
    if mode not in ("mock", "live"):
        raise _fail("--mode は mock か live を指定してください。", 2)
    if filter_mode not in (None, "on", "off", "both"):
        raise _fail("--filter は on / off / both のいずれかで指定してください。", 2)
    if compose not in (None, "single", "two_stage"):
        raise _fail("--compose は single か two_stage を指定してください。", 2)
    if sweep is not None:
        if sweep != "types":
            raise _fail("--sweep は types を指定してください。", 2)
        if mode == "live":
            # 同じ入力に対する、16タイプの下書きだけを並べる(文体カードの差を、人が確認する。§9.6)
            for type_id, name, reply in _run_live(lambda llm: sim.compare_type_drafts(llm, text)):
                print(f"## {type_id} {name}\n{reply}\n")
            return
        rows = asyncio.run(sim.sweep_types())
        problems = [m for row in rows for m in (*row.violations, *row.failures)]
        if as_json:
            print(json.dumps([vars(row) for row in rows], ensure_ascii=False, indent=2))
        else:
            for line in sim.render_sweep(rows):
                print(line)
            print(f"\n{len(rows)}通りを実行。違反・期待と違う点: {len(problems)}件。")
            for message in problems[:20]:
                print(f"  {message}")
        if problems or not all(row.in_range for row in rows):
            raise typer.Exit(1)
        return

    if character_id is None:
        raise _fail("キャラクタのIDを指定してください(全数確認は --sweep types)。", 2)
    _, raw = _load(character_id, root)
    lint_report = lint(raw)
    if lint_report.definition is None or not lint_report.ok:
        _print_findings(lint_report)
        raise _fail("定義にエラーがあるため、シミュレーションを実行できません。")
    persona = sim.persona_from_definition(lint_report.definition)
    scenarios = sim.scenarios_for(character_id, root)
    if scenario:
        wanted = [name.strip() for name in scenario.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in {s.id for s in scenarios}]
        if unknown:
            raise _fail(f"シナリオが見つかりません: {', '.join(unknown)}", 2)
        scenarios = [s for s in scenarios if s.id in wanted]
    try:
        starts: list[float | None] = (
            [float(v) for v in relationship.split(",") if v.strip()] if relationship else [None]
        )
    except ValueError:
        raise _fail("--relationship は、数値をカンマ区切りで指定してください。", 2) from None

    ok = True
    results: list[dict[str, Any]] = []
    for start in starts:
        minor = True if under_18 else None
        if mode == "live":
            reports = _run_live(
                lambda llm, start=start, minor=minor: sim.run_scenarios(
                    persona, scenarios, relationship=start, under_18=minor, live=llm, filter_mode=filter_mode,
                    compose_mode=compose,
                )
            )
        else:
            reports = asyncio.run(
                sim.run_scenarios(persona, scenarios, relationship=start, under_18=minor, compose_mode=compose)
            )
        ok = ok and all(r.ok for r in reports)
        distribution = sim.mode_distribution(reports)
        if as_json:
            results.append(
                {"relationship": start, "modes": distribution, "scenarios": [r.as_dict() for r in reports]}
            )
            continue
        title = "キャラクタの初期値" if start is None else f"{start:+.0f}"
        print(f"# {lint_report.definition.name}({character_id})  開始時の関係値: {title}\n")
        for report in reports:
            for line in sim.render_report(report):
                print(line)
        shares = " / ".join(f"{name} {share:.0%}" for name, share in distribution.items())
        passed = sum(1 for r in reports if r.ok)
        print(f"応答モードの分布: {shares}")
        print(f"合格 {passed}/{len(reports)}。無言率 {distribution['silence']:.0%}。\n")
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
    if not ok:
        raise typer.Exit(1)


def _admin_connection() -> Any:
    """キャラクタ定義を変更するための接続(テーブルの所有者。`az login` 済みであること)。"""
    import psycopg

    from mycompanion.config import ConfigError, PostgresConfig
    from mycompanion.db.postgres import default_token_provider

    try:
        config = PostgresConfig.from_env()
    except ConfigError as exc:
        raise _fail(str(exc), 2) from None
    return psycopg.connect(
        host=config.host, port=config.port, dbname=config.database, user=config.user, sslmode=config.sslmode,
        password=default_token_provider()(),
    )  # fmt: skip


def _checked(character_id: str, root: Path | None) -> tuple[Path, dict[str, Any], LintReport, list[Any]]:
    """定義を読み、lint と、標準・専用シナリオ(`mock`)を実行する。"""
    path, raw = _load(character_id, root)
    report = lint(raw)
    scenarios: list[Any] = []
    if report.definition is not None and report.ok:
        persona = sim.persona_from_definition(report.definition)
        scenarios = asyncio.run(sim.run_scenarios(persona, sim.scenarios_for(character_id, root)))
    return path, raw, report, scenarios


def _stop(reasons: list[str], action: str) -> None:
    if reasons:
        print(f"{action}できません:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        raise typer.Exit(1)


@app.command()
def submit(character_id: str, root: RootOption = None) -> None:
    """レビューに提出する(draft → review)。lint にエラーがなく、シナリオが合格していること。"""
    path, raw, report, scenarios = _checked(character_id, root)
    _stop(pub.blocking_reasons(str(raw.get("status", "draft")), report, scenarios, for_publish=False), "提出")
    raw["status"] = "review"
    pio.save_raw(path, raw)
    passed = sum(1 for r in scenarios if r.ok)
    print(f"レビューに提出しました(lint の警告 {len(report.warnings)}件、シナリオ {passed}/{len(scenarios)} 合格)。")
    for finding in report.warnings:
        print(f"  [{finding.code}] {finding.message}")


@app.command("publish")
def publish_persona(
    character_id: str,
    version: Annotated[str, typer.Option("--version", help="公開するバージョン(X.Y.Z)")],
    reviewer: Annotated[str | None, typer.Option("--reviewer", help="レビュー担当者")] = None,
    notes: Annotated[str | None, typer.Option("--notes", help="確認のメモ(警告があるときは必須)")] = None,
    designer: Annotated[str | None, typer.Option("--designer", help="設計者(W04〜W06 の警告があるときは必須)")] = None,
    root: RootOption = None,
) -> None:
    """公開する(review → published)。公開した版は不変で、変更は新しい版として作る。"""
    path, raw, report, scenarios = _checked(character_id, root)
    _stop(pub.blocking_reasons(str(raw.get("status", "draft")), report, scenarios, reviewer, notes, designer), "公開")
    assert report.definition is not None and reviewer is not None
    passed = sum(1 for r in scenarios if r.ok)
    try:
        with _admin_connection() as conn:
            info = pub.publish(
                conn, report.definition, version, reviewer, notes, report, f"mock: {passed}/{len(scenarios)} 合格"
            )
    except pub.PublishError as exc:
        raise _fail(str(exc)) from None
    raw["status"], raw["version"] = "published", version
    raw["review"] = {"reviewer": reviewer, "reviewed_at": datetime.now(UTC).isoformat(), "notes": notes}
    pio.save_raw(path, raw)
    print(f"公開しました: {character_id} {info.label}(内部の版 {info.version})。")
    print("次に変更するときは、`companion persona set` で編集してから、新しいバージョンで提出・公開してください。")


@app.command()
def rollback(
    character_id: str,
    to: Annotated[str, typer.Option("--to", help="戻す先のバージョン(X.Y.Z)")],
) -> None:
    """以前の公開バージョンに戻す。関係値や記憶などの、ユーザーの状態は保持される。"""
    try:
        with _admin_connection() as conn:
            info = pub.rollback(conn, character_id, to)
    except pub.PublishError as exc:
        raise _fail(str(exc)) from None
    print(f"戻しました: {character_id} は {info.label} が公開版です(それより新しい版は、非推奨にしました)。")


@app.command()
def deprecate(character_id: str) -> None:
    """非推奨にする。会話では、使えなくなる。"""
    with _admin_connection() as conn:
        count = pub.deprecate(conn, character_id)
    print(f"非推奨にしました: {character_id}({count}件の版)。" if count else "公開中の版は、ありません。")


@app.command("versions")
def list_versions(character_id: str, as_json: JsonOption = False) -> None:
    """登録されている版の一覧。"""
    with _admin_connection() as conn:
        items = pub.versions(conn, character_id)
    if as_json:
        print(json.dumps([{"version": v.version, "label": v.label, "status": v.status} for v in items], ensure_ascii=False))
        return
    if not items:
        print("登録されている版は、ありません。")
    for item in items:
        print(f"  {item.label or '-':<10} {item.status:<11} 内部の版 {item.version}  {str(item.created_at)[:16]}")


@app.command("diff")
def diff_versions(character_id: str, first: str, second: str) -> None:
    """2つの版の差分を表示する。"""
    try:
        with _admin_connection() as conn:
            changes = pub.diff(conn, character_id, first, second)
    except pub.PublishError as exc:
        raise _fail(str(exc)) from None
    if not changes:
        print("差分は、ありません。")
    for key, old, new in changes:
        print(f"  {key}:\n    - {old}\n    + {new}")


@app.command("profiles")
def profiles() -> None:
    """表出プロファイル(名前つきプリセット)の一覧。"""
    for key, deltas in PROFILE_PRESETS.items():
        listing = "  ".join(f"{name} {value:+.2f}" for name, value in deltas.items())
        print(f"  {key:<13} {PROFILE_NAMES[key]:<6} {listing}")


# ---------------------------------------------------------------------------
# 好き嫌いシート(§5.4.0)と、好みカタログ
# ---------------------------------------------------------------------------

catalog_app = typer.Typer(help="好みカタログ(好き嫌いシートの選択肢)", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")


@catalog_app.command("list")
def catalog_list(
    category: Annotated[
        str | None, typer.Option("--category", help="区分(color / food / event / life / hobby / action)")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """好みカタログの一覧(ID、表示名、照合のヒント、好き・嫌いに選べるか)。"""
    if category is not None and category not in catalog.CATEGORIES:
        raise _fail(f"区分は {' / '.join(catalog.CATEGORIES)} のいずれかで指定してください。", 2)
    entries = catalog.items(category)
    if as_json:
        rows = [
            {
                "id": e.id, "label": e.label, "include": list(e.include), "exclude": list(e.exclude),
                "likeable": e.likeable, "dislikeable": e.dislikeable, "involvement": e.involvement,
            }
            for e in entries
        ]  # fmt: skip
        print(json.dumps({"catalog_version": catalog.CATALOG_VERSION, "items": rows}, ensure_ascii=False, indent=2))
        return
    print(f"好みカタログ(版 {catalog.CATALOG_VERSION}、{len(entries)}項目)")
    current = None
    for entry in entries:
        if entry.category != current:
            current = entry.category
            print(f"\n[{catalog.CATEGORY_LABELS[current]}]")
        side = "好き・嫌い" if entry.likeable and entry.dislikeable else "好きのみ" if entry.likeable else "嫌いのみ"
        note = catalog.note_text(entry)
        mark = " ※関わり方を判定" if entry.involvement else ""
        print(f"  {entry.id:<24} {entry.label}({side}){mark}" + (f"  {note}" if note else ""))


def _sheet_rows(definition: Any) -> list[dict[str, Any]]:
    """シートの各欄と、解決後のルール(1回目の最大変化量つき)。"""
    rows = []
    for rule in sheet_module.resolve_rules(definition.likes_dislikes):
        first = sheet_module.first_delta(rule, definition.temperament.reactivity)
        rows.append({**rule, "first_delta": round(first, 3)})
    return rows


@app.command()
def likes(
    character_id: str,
    try_text: Annotated[
        str | None, typer.Option("--try", help="発話を1つ入力して、各項目の照合と、発火の有無を確認する(LLM を1回呼ぶ)")
    ] = None,
    mock: Annotated[bool, typer.Option("--mock", help="--try で、LLM を呼ばずに、模擬の判定を使う")] = False,
    root: RootOption = None,
    as_json: JsonOption = False,
) -> None:
    """好き嫌いシートの内容と、解決後の反応を表示する。`--try` で、発話への反応を確認する。"""
    _, raw = _load(character_id, root)
    report = lint(raw)
    if report.definition is None or not report.ok:
        _print_findings(report)
        raise _fail("定義にエラーがあるため、シートを解決できません。")
    definition = report.definition
    rows = _sheet_rows(definition)
    if try_text is None:
        if as_json:
            print(json.dumps({"likes_dislikes": raw.get("likes_dislikes"), "resolved": rows}, ensure_ascii=False, indent=2))
            return
        print(f"{definition.name}({definition.character_id})の好き嫌いシート")
        if not rows:
            print("  (何も選ばれていません。`companion persona catalog list` で選択肢を確認できます)")
        for row in rows:
            side = "好き" if row["kind"] == "like" else "嫌い"
            effects = " ".join(f"{k} {v:.1f}" for k, v in row["effects"].items())
            extra = [f"繰り返し: {row['repeat']}"]
            if row.get("scope") == "with_character":
                extra.append("あなたとの間の行為のときだけ")
            if row.get("avoid"):
                extra.append("一定量で話題を変えようとする")
            cut = "(上限 0.25)" if row["first_delta"] > 0.25 else ""
            print(
                f"  {side} {catalog.CATEGORY_LABELS[row['category']]}: {row['topic']}({row['catalog_id']}) "
                f"gain {row['gain']:.1f} / {effects} / 1回目の最大 {row['first_delta']:.2f}{cut} / {'、'.join(extra)}"
            )
        return

    persona = sim.persona_from_definition(definition)
    context = EmotionContext(
        rule_topics=[str(rule.get("topic", "")) for rule in persona.appraisal_rules],
        rule_notes=[str(rule.get("note", "")) for rule in persona.appraisal_rules],
    )

    async def job(llm: Any) -> Any:
        return await llm.estimate_emotion(try_text, context)

    result = asyncio.run(job(MockLLMClient())) if mock else _run_live(job)
    settings = EngineSettings()
    outcome = appraisal.evaluate(
        persona.appraisal_rules,
        appraisal.SheetContext(
            matches=dict(result.match_details),
            turn_no=1,
            risk_sexual=result.estimate.risk.sexual,
            risk_violence=result.estimate.risk.violence,
            crisis=result.estimate.risk.self_harm >= 0.5,
        ),
        None,
        definition.temperament.reactivity,
        settings,
    )
    if as_json:
        details = {str(i): m.model_dump() for i, m in result.match_details.items()}
        print(json.dumps({"matches": details, "hits": outcome.hits, "delta": outcome.categories}, ensure_ascii=False, indent=2))
        return
    print(f"発話: {try_text}")
    if not outcome.hits:
        print("  どの項目にも、関係しないと判定されました(関連度が下限未満)。")
    for hit in outcome.hits:
        head = f"  {hit['catalog_id']}: 関連度 {hit['relevance']:.2f} / 極性 {hit['polarity']:+.2f} / 関わり方 {hit['involvement']}"
        if "delta" in hit:
            changes = " ".join(f"{k} {v:+.2f}" for k, v in hit["delta"].items())
            print(f"{head} → 発火({changes})")
        else:
            print(f"{head} → 発火しない(理由: {hit['suppressed_by']})")
    print("  ※1回目の発話としての判定です(繰り返しの調整と、苦痛状態のガードは含みません)。")
