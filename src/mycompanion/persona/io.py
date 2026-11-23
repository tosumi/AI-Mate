"""ペルソナ定義ファイルの読み書きと、実行時の形への変換(ペルソナデザイナ仕様書 §4.1、§14)。

編集用の形式は YAML(`personas/<character_id>/persona.yaml`)。
エンジンへの取り込み時に、解決済みの値(`traits`)を計算し、`characters` テーブルの形にする。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from mycompanion.persona import types as ptypes
from mycompanion.persona.resolve import (
    PROFILE_INITIAL_RELATIONSHIP,
    PROFILE_PRESETS,
    PROFILE_TEMPERAMENT,
    merged_preferences,
    resolve,
    resolve_association,
)
from mycompanion.persona.schema import PersonaDefinition
from mycompanion.persona.sheet import resolve_rules

PERSONAS_DIR = Path("personas")
FILE_NAME = "persona.yaml"


def persona_path(character_id: str, root: Path | None = None) -> Path:
    return (root or PERSONAS_DIR) / character_id / FILE_NAME


def load_raw(path: Path) -> dict[str, Any]:
    """YAML を辞書として読む(検査の前の、そのままの内容)。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"ペルソナ定義の形式が不正です: {path}")  # noqa: TRY004 - 内容の不備として扱う
    return data


def load(path: Path) -> PersonaDefinition:
    return PersonaDefinition.model_validate(load_raw(path))


def dump(raw: dict[str, Any]) -> str:
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100)


def save_raw(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(raw), encoding="utf-8")


def list_ids(root: Path | None = None) -> list[str]:
    base = root or PERSONAS_DIR
    if not base.exists():
        return []
    return sorted(p.parent.name for p in base.glob(f"*/{FILE_NAME}"))


def new_definition(
    character_id: str, name: str, type_id: str = "iaef", profile: str | None = None
) -> dict[str, Any]:
    """新しいペルソナ定義のひな形(デザイナ仕様書 付録A の `blank` に相当)。"""
    if not ptypes.is_type_id(type_id):
        raise ValueError(f"16タイプの ID ではありません: {type_id}")
    if profile is not None and profile not in PROFILE_PRESETS:
        raise ValueError(f"表出プロファイルのプリセットにありません: {profile}")
    reactivity, recovery_rate, (v, a, d) = PROFILE_TEMPERAMENT.get(profile or "", (0.5, 0.3, (0.0, 0.0, 0.0)))
    return {
        "schema_version": 1,
        "character_id": character_id,
        "version": "0.1.0",
        "status": "draft",
        "name": name,
        "concept": {
            "one_line": "",
            "role": "",
            "core_trait": "",
            "contrast": "",
            "relationship_start": "",
            "avoid": "",
        },
        "persona_prompt": "(ここに、性格ディレクティブを書く)",
        "speech_style": "(ここに、話し方を書く)",
        "base_personality": {
            "type": type_id,
            "axes": ptypes.preset_axes(type_id),
            "source": "preset",
            "quiz": None,
        },
        "profile": {"preset": profile, "deltas": dict(PROFILE_PRESETS.get(profile or "", {}))},
        "overrides": {},
        "initial_relationship": PROFILE_INITIAL_RELATIONSHIP.get(profile or "", 0.0),
        "adaptation": {
            "adaptability": 0.5,
            "adjustable": ["tone_softness", "distance", "talk_amount", "question_frequency", "humor"],
            "fixed": ["first_person", "speech_endings", "catchphrases", "core_values", "character_name"],
        },
        "preferences": [],
        "temperament": {
            "baseline_vad": {"v": v, "a": a, "d": d},
            "reactivity": reactivity,
            "recovery_rate": recovery_rate,
        },
        "silence_policy": {"allow_anger_silence": False, "max_consecutive": 2},
        "appraisal_rules": [],
        "likes_dislikes": {
            "catalog_version": 2,
            "liked": dict.fromkeys(("color", "food", "event", "life", "hobby", "action")),
            "disliked": dict.fromkeys(("color", "food", "event", "life", "hobby", "action")),
        },
        "review": {"reviewer": None, "reviewed_at": None, "notes": None},
    }


def set_value(raw: dict[str, Any], dotted_key: str, value: Any) -> None:
    """`profile.deltas.talkativeness=-0.1` のような指定で、値を設定する。"""
    parts = dotted_key.split(".")
    node = raw
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        if not isinstance(child, dict):
            raise KeyError(f"設定できないキーです: {dotted_key}")
        node = child
    node[parts[-1]] = value
    # 軸を手で変えたら、決め方は custom になり、type は最も近いタイプに直す(§7.2.2)
    if parts[:2] == ["base_personality", "axes"]:
        base = raw["base_personality"]
        base["source"] = "custom"
        base["type"] = ptypes.type_id_from_axes(base["axes"])


def parse_value(text: str) -> Any:
    """コマンドラインの値を、YAML の規則で解釈する(数値、真偽、null、文字列)。"""
    return yaml.safe_load(text)


def to_character_row(definition: PersonaDefinition) -> dict[str, Any]:
    """`characters` テーブルの1行(JSON の列は辞書のまま)。`traits` は、ここで解決して固定する。"""
    traits = resolve(definition).as_dict()
    traits["silence_policy"] = definition.silence_policy.model_dump()
    traits |= resolve_association(definition)  # 連想のしやすさ・話題の移りやすさ(§9.13)
    base = definition.base_personality.model_dump(mode="json")
    return {
        "character_id": definition.character_id,
        "version_label": definition.version,
        "name": definition.name,
        "persona_prompt": definition.persona_prompt.strip(),
        "speech_style": definition.speech_style.strip(),
        "initial_relationship": definition.initial_relationship,
        "base_personality_json": base,
        "profile_json": definition.profile.model_dump(),
        "overrides_json": definition.overrides,
        "traits_json": traits,
        "preferences_json": merged_preferences(definition),
        "adaptation_json": definition.adaptation.model_dump(),
        "temperament_json": definition.temperament.model_dump(),
        # 手書きのルールのあとに、好き嫌いシートを解決したルール(origin: sheet)を並べる
        "appraisal_rules_json": [
            *(rule.model_dump() for rule in definition.appraisal_rules),
            *resolve_rules(definition.likes_dislikes),
        ],
        "likes_dislikes_json": (
            definition.likes_dislikes.model_dump(exclude_none=True) if definition.likes_dislikes else None
        ),
        "event_rules_json": [rule.model_dump(exclude_none=True) for rule in definition.event_rules],
        "concept_json": definition.concept,
        "status": definition.status,
    }
