"""デザイナの Web 画面のための処理(本体仕様書 §15.1 の M10 の手順2。ペルソナデザイナ仕様書 §16 の v1.5)。

- 一般ユーザーにも開く。**状態を持たず、DB に書かない**(下書きは、利用者の端末の YAML ファイル)。
- CLI と同じ処理(lint・解決・診断・模擬のシミュレーション)を呼び、結果を一致させる。
- 実際のモデル(`live`)は使わない(一般ユーザーは模擬だけ。2026-11-25 の決定)。
- 定義の本文は、ログに出さない。
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import yaml

from mycompanion.config import EngineSettings
from mycompanion.emotion.models import CATEGORY_NAMES
from mycompanion.engine.planner import base_length
from mycompanion.persona import io as pio
from mycompanion.persona import quiz
from mycompanion.persona import sheet as sheet_module
from mycompanion.persona import simulate as sim
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import LintReport, lint
from mycompanion.persona.resolve import (
    PROFILE_INITIAL_RELATIONSHIP,
    PROFILE_NAMES,
    PROFILE_PRESETS,
    PROFILE_TEMPERAMENT,
    default_traits,
    effective_traits,
    merged_preferences,
    resolve,
    resolve_association,
)
from mycompanion.state import catalog
from mycompanion.state.events import EVENT_CATALOG
from mycompanion.state.relationship import sync_weight

SAMPLE_PREFIX = "sample_"
YAML_MAX_CHARS = 200_000
CURVE_POINTS = (-20.0, -10.0, 0.0, 10.0, 20.0)
SYNC_POINTS = (-20.0, -10.0, 0.0, 5.0, 10.0, 15.0, 20.0)


class DesignerError(Exception):
    """利用者に、そのまま伝えてよい失敗(定義の内容は含めない)。"""


# ---------------------------------------------------------------------------
# YAML(CLI と同じ形式。別名・参照は受け付けない)
# ---------------------------------------------------------------------------


class _NoAliasLoader(yaml.SafeLoader):
    """別名(`*name`)を使わせない。小さなファイルから、巨大なデータを作らせないため。"""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise DesignerError("YAML の別名(*)は使えません。")
        return super().compose_node(parent, index)


def parse_yaml(text: str) -> dict[str, Any]:
    if len(text) > YAML_MAX_CHARS:
        raise DesignerError("ファイルが大きすぎます。")
    try:
        raw = yaml.load(text, Loader=_NoAliasLoader)  # SafeLoader の派生(別名も禁止)
    except DesignerError:
        raise
    except yaml.YAMLError:
        raise DesignerError("YAML として読めませんでした。") from None
    if not isinstance(raw, dict):
        raise DesignerError("ペルソナ定義の YAML ではありません。")
    return raw


def dump_yaml(raw: dict[str, Any]) -> str:
    return pio.dump(raw)


# ---------------------------------------------------------------------------
# 選択肢(性格タイプ・表出プロファイル・好みカタログ・イベントのトリガー)
# ---------------------------------------------------------------------------


def choices() -> dict[str, Any]:
    types = []
    for item in ptypes.all_types():
        types.append({
            "type_id": item.type_id, "name": item.name, "composition": item.composition, "axes": item.axes,
            "style_card": item.style_card, "defaults": default_traits(item.axes["i"], item.axes["e"]).as_dict(),
        })  # fmt: skip
    profiles = []
    for key, deltas in PROFILE_PRESETS.items():
        reactivity, recovery_rate, (v, a, d) = PROFILE_TEMPERAMENT[key]
        profiles.append({
            "id": key, "name": PROFILE_NAMES[key], "deltas": deltas,
            "initial_relationship": PROFILE_INITIAL_RELATIONSHIP[key],
            "temperament": {"reactivity": reactivity, "recovery_rate": recovery_rate, "baseline_vad": {"v": v, "a": a, "d": d}},
        })  # fmt: skip
    return {
        "axes": [
            {"id": axis, "name": ptypes.AXIS_NAMES[axis], "low": ptypes.AXIS_POLES[axis][0][1], "high": ptypes.AXIS_POLES[axis][1][1]}
            for axis in ptypes.AXES
        ],
        "types": types,
        "profiles": profiles,
        "emotions": list(CATEGORY_NAMES),
        "likes_catalog": {
            "version": catalog.CATALOG_VERSION,
            "categories": [{"id": c, "label": catalog.CATEGORY_LABELS[c]} for c in catalog.CATEGORIES],
            "items": [
                {
                    "id": e.id, "category": e.category, "label": e.label, "likeable": e.likeable,
                    "dislikeable": e.dislikeable, "involvement": e.involvement, "note": catalog.note_text(e),
                }
                for e in catalog.items()
            ],
        },
        "event_triggers": [
            {
                "name": t.name, "kind": t.kind, "label": t.label, "positive_only": t.positive_only,
                "requires_character_target": t.requires_character_target,
            }
            for t in EVENT_CATALOG.values()
        ],
    }


def new_definition(character_id: str, name: str, type_id: str, profile: str | None) -> dict[str, Any]:
    try:
        return pio.new_definition(character_id, name, type_id, profile)
    except ValueError as exc:
        raise DesignerError(str(exc)) from None


# ---------------------------------------------------------------------------
# サンプル(読み取り専用の参考)
# ---------------------------------------------------------------------------


def _samples_root(root: Path | None) -> Path:
    return root or pio.PERSONAS_DIR


def sample_ids(root: Path | None = None) -> list[str]:
    return [i for i in pio.list_ids(_samples_root(root)) if i.startswith(SAMPLE_PREFIX)]


def samples(root: Path | None = None) -> list[dict[str, str]]:
    out = []
    for character_id in sample_ids(root):
        raw = pio.load_raw(pio.persona_path(character_id, _samples_root(root)))
        out.append({"character_id": character_id, "name": str(raw.get("name", character_id))})
    return out


def sample(character_id: str, root: Path | None = None) -> dict[str, Any]:
    if character_id not in sample_ids(root):  # 一覧にある ID だけ(パスを組み立てる前に確かめる)
        raise DesignerError("サンプルが見つかりません。")
    raw = pio.load_raw(pio.persona_path(character_id, _samples_root(root)))
    return {"character_id": character_id, "read_only": True, "definition": raw, "yaml": dump_yaml(raw)}


# ---------------------------------------------------------------------------
# 検査と解決(lint、解決済みの値、派生指標)
# ---------------------------------------------------------------------------


def _findings(report: LintReport) -> list[dict[str, str]]:
    return [{"code": f.code, "level": f.level, "message": f.message, "path": f.path} for f in report.findings]


def check(raw: dict[str, Any], settings: EngineSettings | None = None, root: Path | None = None) -> dict[str, Any]:
    """lint と、解決済みの値・派生指標(デザイナ仕様書 §6)。画面の編集のたびに呼ぶ(軽い処理だけ)。"""
    settings = settings or EngineSettings()
    report = lint(raw, modulation_max=settings.relationship_modulation_max)
    result: dict[str, Any] = {
        "ok": report.ok,
        "findings": _findings(report),
        # サンプルの ID のままでは、公開のときに、サンプルと取り違える。保存の前に ID を変えてもらう
        "uses_sample_id": str(raw.get("character_id", "")) in sample_ids(root),
        "resolved": None,
    }
    definition = report.definition
    if definition is None:
        return result
    traits = resolve(definition)
    axes = definition.base_personality.axes
    recovery = definition.temperament.recovery_rate
    curve = []
    for r in CURVE_POINTS:
        eff = effective_traits(traits, r, settings.relationship_modulation_max)
        curve.append({
            "relationship": r, "talkativeness_eff": round(eff.talkativeness_eff, 3),
            "expressiveness_eff": round(eff.expressiveness_eff, 3), "length": base_length(eff.talkativeness_eff),
            "expression_threshold": round(eff.expression_threshold, 3), "expression_cap": round(eff.expression_cap, 3),
        })  # fmt: skip
    sheet_rows = [
        {**rule, "first_delta": round(sheet_module.first_delta(rule, definition.temperament.reactivity), 3)}
        for rule in sheet_module.resolve_rules(definition.likes_dislikes)
    ]
    result["resolved"] = {
        "type_id": definition.base_personality.type,
        "type_from_axes": ptypes.type_id_from_axes(axes.model_dump()),
        "traits": traits.as_dict(),
        "defaults": default_traits(axes.i, axes.e).as_dict(),
        "association": resolve_association(definition),
        "style_card": ptypes.style_card(axes.model_dump()),
        "preferences": merged_preferences(definition),
        "curve": curve,
        "sync_curve": [{"relationship": r, "weight": round(sync_weight(r, settings), 3)} for r in SYNC_POINTS],
        # 感情を引きずるターン数の目安: ln(0.5) / ln(1 − recovery_rate)
        "half_life_turns": round(math.log(0.5) / math.log(1 - recovery), 2) if 0 < recovery < 1 else None,
        "sheet_rules": sheet_rows,
    }
    return result


# ---------------------------------------------------------------------------
# 診断(方式B。想定するキャラクタについて答える。利用者自身の性格は扱わない)
# ---------------------------------------------------------------------------


def quiz_questions() -> dict[str, Any]:
    def view(q: quiz.Question) -> dict[str, Any]:
        return {"id": q.id, "axis": q.axis, "text": q.text}

    return {
        "bank_version": quiz.BANK_VERSION,
        "preamble": quiz.PREAMBLE,
        "disclaimer": quiz.DISCLAIMER,
        "main": [view(q) for q in quiz.main_questions()],
        "tiebreak": {axis: [view(q) for q in quiz.tiebreak_questions(axis)] for axis in ptypes.AXES},
    }


def quiz_score(answers: dict[str, int], tiebreak_choice: dict[str, str]) -> dict[str, Any]:
    try:
        result = quiz.score(answers, tiebreak_choice)
    except ValueError as exc:
        raise DesignerError(str(exc)) from None
    out: dict[str, Any] = {
        "type_id": result.type_id,
        "type_name": ptypes.get_type(result.type_id).name if result.type_id else None,
        "axes": {a: vars(r) for a, r in result.axes.items()},
        "undecided_axes": result.undecided_axes,
        "warnings": result.warnings,
        "preset_axes": ptypes.preset_axes(result.type_id) if result.type_id else None,
        "score_axes": quiz.axes_from_scores(result) if result.type_id else None,
    }
    if result.type_id:
        # CLI の `type quiz` と同じ形で、定義に入れる値(採用方法ごと)
        out["base_personality"] = {
            adopt: {
                "type": result.type_id, "axes": out["preset_axes"] if adopt == "preset" else out["score_axes"],
                "source": "quiz",
                "quiz": {
                    "bank_version": quiz.BANK_VERSION, "answers": answers,
                    "scores": {a: r.score for a, r in result.axes.items()},
                    "strength": {a: r.strength for a, r in result.axes.items()},
                    "borderline": [a for a, r in result.axes.items() if r.used_tiebreak or r.borderline],
                    "tiebreak_choice": tiebreak_choice, "adopt": adopt,
                },
            }
            for adopt in ("preset", "scores")
        }  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# 模擬のシミュレーション(標準シナリオ。LLM を呼ばない)
# ---------------------------------------------------------------------------


def simulate(raw: dict[str, Any], relationships: list[float | None], under_18: bool) -> dict[str, Any]:
    """標準シナリオを `mock` で実行する(同期の処理。呼び出し側で、別のスレッドで動かす)。"""
    report = lint(raw)
    if report.definition is None or not report.ok:
        raise DesignerError("定義にエラーがあるため、シミュレーションを実行できません。")
    persona = sim.persona_from_definition(report.definition)
    scenarios = sim.load_scenarios()  # 標準シナリオだけ(専用シナリオは、運営者の CLI で)
    runs = []
    for start in relationships:
        reports = asyncio.run(
            sim.run_scenarios(persona, scenarios, relationship=start, under_18=True if under_18 else None)
        )
        runs.append({
            "relationship": start, "modes": sim.mode_distribution(reports),
            "passed": sum(1 for r in reports if r.ok), "total": len(reports),
            "scenarios": [r.as_dict() for r in reports],
        })  # fmt: skip
    return {"ok": all(run["passed"] == run["total"] for run in runs), "runs": runs}


def sweep() -> list[dict[str, Any]]:
    """16タイプ × 表出プロファイルの全数確認(だれが呼んでも同じ結果。呼び出し側で保持する)。"""
    return [vars(row) for row in asyncio.run(sim.sweep_types())]
