"""開発用のデータを登録する(dev だけで使う)。

- `personas/` のペルソナ定義(本体仕様書 付録C のサンプル4体)を、検査(lint)してから登録する。
  解決済みの値(`traits`)は、登録時に計算して固定する。エラーのある定義は登録しない。
- 16タイプの定義と、診断の質問バンクを登録する。

正式な公開(レビュー、承認、バージョン管理)は、M5 のペルソナデザイナの手順で行う。
テーブルの所有者(管理者ロール)で実行する。何度実行しても同じ結果になる。

    uv run python -m mycompanion.db.seed_dev
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from mycompanion.config import ConfigError, PostgresConfig, environment_name
from mycompanion.db.postgres import default_token_provider
from mycompanion.persona import io as persona_io
from mycompanion.persona import quiz
from mycompanion.persona import types as ptypes
from mycompanion.persona.lint import lint

DEV_VERSION = 1  # 開発用の登録は、常に版 1 を上書きする


def seed_reference_data(conn: psycopg.Connection) -> None:
    """16タイプの定義と、質問バンク。"""
    for item in ptypes.all_types():
        conn.execute(
            "INSERT INTO personality_types (type_id, name, axes_json, style_card, summary, data_version) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (type_id) DO UPDATE SET name = EXCLUDED.name, "
            "axes_json = EXCLUDED.axes_json, style_card = EXCLUDED.style_card, summary = EXCLUDED.summary, "
            "data_version = EXCLUDED.data_version",
            (item.type_id, item.name, Jsonb(item.axes), item.style_card, item.composition, ptypes.DATA_VERSION),
        )
    for question in quiz.QUESTIONS:
        low, high = ptypes.AXIS_POLES[question.axis]
        conn.execute(
            "INSERT INTO quiz_bank (bank_version, question_id, axis, keyed_pole, reverse, text, is_tiebreak) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (bank_version, question_id) DO UPDATE SET "
            "axis = EXCLUDED.axis, keyed_pole = EXCLUDED.keyed_pole, reverse = EXCLUDED.reverse, "
            "text = EXCLUDED.text, is_tiebreak = EXCLUDED.is_tiebreak",
            (
                quiz.BANK_VERSION,
                question.id,
                question.axis,
                (low if question.reverse else high)[0],
                question.reverse,
                question.text,
                question.tiebreak,
            ),
        )


def seed_personas(conn: psycopg.Connection, root: Path | None = None) -> tuple[list[str], list[str]]:
    """ペルソナ定義を登録する。(登録した ID, エラーで登録しなかった ID)を返す。"""
    registered, rejected = [], []
    for character_id in persona_io.list_ids(root):
        report = lint(persona_io.load_raw(persona_io.persona_path(character_id, root)))
        if not report.ok or report.definition is None:
            rejected.append(character_id)
            continue
        row = persona_io.to_character_row(report.definition)
        json_columns = [name for name in row if name.endswith("_json")]
        values = {name: Jsonb(value) if name in json_columns else value for name, value in row.items()}
        values["version"] = DEV_VERSION
        values["status"] = "published"  # dev では、検査に通ったものを公開済みとして扱う
        columns = list(values)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in ("character_id", "version"))
        conn.execute(
            f"INSERT INTO characters ({', '.join(columns)}) "
            f"VALUES ({', '.join('%(' + c + ')s' for c in columns)}) "
            f"ON CONFLICT (character_id, version) DO UPDATE SET {updates}",
            values,
        )
        registered.append(character_id)
    return registered, rejected


def main() -> int:
    if environment_name() == "prod":
        print("エラー: 本番環境では実行できません。", file=sys.stderr)
        return 2
    try:
        config = PostgresConfig.from_env()
    except ConfigError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        sslmode=config.sslmode,
        password=default_token_provider()(),
    ) as conn:
        seed_reference_data(conn)
        print(f"登録  性格タイプ {len(ptypes.all_types())}件、質問バンク {len(quiz.QUESTIONS)}問")
        registered, rejected = seed_personas(conn)
        for character_id in registered:
            print(f"登録  {character_id}")
        for character_id in rejected:
            print(f"除外  {character_id}(検査でエラー。`companion persona validate` で確認)", file=sys.stderr)
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
