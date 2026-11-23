"""ペルソナのバージョン管理と公開(ペルソナデザイナ仕様書 §12)。

    draft → review → published → deprecated

- 公開済みの版は、不変。変更は、新しい版として作る。
- 公開には、①lint にエラーがない、②標準シナリオと専用シナリオが合格、③警告があればレビューのメモ、
  ④恋愛・依存などに関わる警告(W04〜W06)があれば、設計者とは別のレビュー担当者、が必要(§12.4)。
- 承認の記録(レビュー担当者、日時、メモ、lint の結果)は、`character_reviews` に保存する。
- ロールバックは、指定の版より新しい版を非推奨にして、指定の版を公開状態に戻す。
  関係値・記憶などのユーザーの状態は、キャラクタの ID に紐づくので、そのまま引き継がれる。

DB の操作は、テーブルの所有者(管理者ロール)の接続で行う(アプリのロールは、キャラクタ定義を変更できない)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mycompanion.persona import io as persona_io
from mycompanion.persona.lint import LintReport
from mycompanion.persona.schema import PersonaDefinition
from mycompanion.persona.simulate import ScenarioReport

VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# 設計者とは別のレビュー担当者が必要な警告(恋愛的な設定、依存を誘う設定など。§12.4)
SEPARATE_REVIEWER_CODES = ("W04", "W05", "W06")
_JSON_COLUMNS = (
    "base_personality_json", "profile_json", "overrides_json", "traits_json", "preferences_json",
    "adaptation_json", "temperament_json", "appraisal_rules_json", "event_rules_json", "concept_json",
    "likes_dislikes_json",
)  # fmt: skip
_TEXT_COLUMNS = ("name", "persona_prompt", "speech_style", "initial_relationship")


class PublishError(Exception):
    """公開の手順のエラー(利用者に、そのまま伝える)。"""


def parse_version(label: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.match(label or "")
    if not match:
        raise PublishError(f"バージョンは X.Y.Z の形式で指定してください: {label}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def blocking_reasons(
    status: str,
    lint_report: LintReport,
    scenario_reports: list[ScenarioReport],
    reviewer: str | None = None,
    notes: str | None = None,
    designer: str | None = None,
    for_publish: bool = True,
) -> list[str]:
    """提出・公開を止める理由の一覧。空なら、進められる(§12.4)。"""
    reasons: list[str] = []
    if lint_report.errors:
        reasons.append(f"lint にエラーが {len(lint_report.errors)}件ある(`companion persona validate` で確認)")
    failed = [r.scenario_id for r in scenario_reports if not r.ok]
    if failed:
        reasons.append(f"シナリオが不合格: {', '.join(failed)}(`companion persona simulate` で確認)")
    if not for_publish:
        if status not in ("draft", "review"):
            reasons.append(f"提出できるのは、draft の定義だけ(現在: {status})")
        return reasons

    if status != "review":
        reasons.append(f"公開できるのは、レビューに提出済み(review)の定義だけ(現在: {status})")
    if not (reviewer or "").strip():
        reasons.append("レビュー担当者(--reviewer)が必要")
    codes = {finding.code for finding in lint_report.warnings}
    if codes and not (notes or "").strip():
        reasons.append(f"警告({', '.join(sorted(codes))})があるので、確認した理由のメモ(--notes)が必要")
    sensitive = sorted(codes & set(SEPARATE_REVIEWER_CODES))
    if sensitive and (not (designer or "").strip() or (designer or "").strip() == (reviewer or "").strip()):
        reasons.append(
            f"警告 {', '.join(sensitive)} があるので、設計者(--designer)とは別の人がレビューする必要がある"
        )
    return reasons


@dataclass(frozen=True)
class VersionInfo:
    version: int  # 内部の連番
    label: str  # X.Y.Z
    status: str
    created_at: datetime | None


def versions(conn: psycopg.Connection, character_id: str) -> list[VersionInfo]:
    rows = conn.execute(
        "SELECT version, coalesce(version_label, ''), status, created_at FROM characters "
        "WHERE character_id = %s ORDER BY version",
        (character_id,),
    ).fetchall()
    return [VersionInfo(int(r[0]), r[1], r[2], r[3]) for r in rows]


def publish(
    conn: psycopg.Connection,
    definition: PersonaDefinition,
    label: str,
    reviewer: str,
    notes: str | None,
    lint_report: LintReport,
    simulation_summary: str = "",
) -> VersionInfo:
    """新しい版として公開する。以前の公開版は、行として残る(不変)。"""
    wanted = parse_version(label)
    existing = versions(conn, definition.character_id)
    published = [parse_version(v.label) for v in existing if v.status in ("published", "deprecated") and v.label]
    if published and wanted <= max(published):
        newest = ".".join(str(n) for n in max(published))
        raise PublishError(f"バージョンは、これまでの公開版({newest})より大きくしてください")
    number = max((v.version for v in existing), default=0) + 1

    row = persona_io.to_character_row(definition)
    values: dict[str, Any] = {k: Jsonb(v) if k in _JSON_COLUMNS else v for k, v in row.items()}
    values |= {"version": number, "version_label": label, "status": "published"}
    columns = list(values)
    conn.execute(
        f"INSERT INTO characters ({', '.join(columns)}) VALUES ({', '.join('%(' + c + ')s' for c in columns)})",
        values,
    )
    conn.execute(
        "INSERT INTO character_reviews (character_id, version, reviewer, reviewed_at, lint_report_json, "
        "simulation_report_ref, notes, decision) VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved')",
        (
            definition.character_id,
            number,
            reviewer,
            datetime.now(UTC),
            Jsonb([{"code": f.code, "level": f.level, "message": f.message} for f in lint_report.findings]),
            simulation_summary,
            notes,
        ),
    )
    return VersionInfo(number, label, "published", None)


def rollback(conn: psycopg.Connection, character_id: str, label: str) -> VersionInfo:
    """指定の版に戻す。それより新しい版は、非推奨にする(行は残す)。"""
    parse_version(label)
    existing = versions(conn, character_id)
    target = next((v for v in existing if v.label == label and v.status in ("published", "deprecated")), None)
    if target is None:
        raise PublishError(f"公開されたことのある版が見つかりません: {character_id} {label}")
    conn.execute(
        "UPDATE characters SET status = 'deprecated' WHERE character_id = %s AND version > %s AND status = 'published'",
        (character_id, target.version),
    )
    conn.execute(
        "UPDATE characters SET status = 'published' WHERE character_id = %s AND version = %s",
        (character_id, target.version),
    )
    conn.execute(
        "INSERT INTO character_reviews (character_id, version, reviewed_at, decision) VALUES (%s, %s, %s, 'rollback')",
        (character_id, target.version, datetime.now(UTC)),
    )
    return VersionInfo(target.version, label, "published", target.created_at)


def deprecate(conn: psycopg.Connection, character_id: str) -> int:
    """公開中の版を、すべて非推奨にする。会話では、使えなくなる。"""
    cursor = conn.execute(
        "UPDATE characters SET status = 'deprecated' WHERE character_id = %s AND status = 'published'",
        (character_id,),
    )
    return cursor.rowcount


def diff(conn: psycopg.Connection, character_id: str, first: str, second: str) -> list[tuple[str, Any, Any]]:
    """2つの版の差分(項目, 前の値, 後の値)。JSON の列は、キーごとに比べる。"""
    columns = (*_TEXT_COLUMNS, *_JSON_COLUMNS)
    rows = {}
    for label in (first, second):
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM characters WHERE character_id = %s AND version_label = %s "
            "ORDER BY version DESC LIMIT 1",
            (character_id, label),
        ).fetchone()
        if row is None:
            raise PublishError(f"版が見つかりません: {character_id} {label}")
        rows[label] = dict(zip(columns, row, strict=True))
    return diff_rows(rows[first], rows[second])


def diff_rows(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    changes: list[tuple[str, Any, Any]] = []
    for column, old in before.items():
        new = after.get(column)
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted({*old, *new}):
                if old.get(key) != new.get(key):
                    changes.append((f"{column.removesuffix('_json')}.{key}", old.get(key), new.get(key)))
        elif old != new:
            changes.append((column.removesuffix("_json"), old, new))
    return changes
