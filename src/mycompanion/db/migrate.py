"""スキーマのマイグレーション(本体仕様書 §11.5.7)。

`migrations/` の SQL を、ファイル名の順に、未適用のものだけ適用する。
テーブルの所有者(管理者ロール)で実行する。アプリのロールでは実行しない。

    uv run python -m mycompanion.db.migrate            # 未適用のものを適用
    uv run python -m mycompanion.db.migrate --status   # 適用状況の表示
    uv run python -m mycompanion.db.migrate --grant-app-identity <マネージド ID の名前>
        # Container Apps のマネージド ID を PostgreSQL のロールにし、アプリのロール(mycompanion_app)を与える
        # (Entra の管理者で実行する。§15.1 の M6-4)
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from importlib import resources

import psycopg

from mycompanion.config import ConfigError, PostgresConfig
from mycompanion.db.postgres import default_token_provider


def list_migrations() -> list[tuple[str, str]]:
    """(ファイル名, SQL)の一覧を、ファイル名の順に返す。"""
    folder = resources.files("mycompanion.db") / "migrations"
    items = [
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    ]
    return sorted(items)


def checksum(sql: str) -> str:
    # 改行コードの違い(CRLF / LF)で値が変わらないようにする
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def run(conn: psycopg.Connection, status_only: bool = False) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.commit()
    applied = dict(conn.execute("SELECT name, checksum FROM schema_migrations").fetchall())

    pending = 0
    for name, sql in list_migrations():
        digest = checksum(sql)
        if name in applied:
            if applied[name] != digest:
                # 適用済みのファイルは書き換えない。変更は新しいファイルで行う
                print(f"エラー: 適用済みのマイグレーションが変更されています: {name}", file=sys.stderr)
                return 1
            print(f"適用済み  {name}")
            continue
        pending += 1
        if status_only:
            print(f"未適用    {name}")
            continue
        with conn.transaction():
            conn.execute(sql)  # type: ignore[arg-type]
            conn.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)", (name, digest)
            )
        print(f"適用      {name}")

    if pending == 0:
        print("未適用のマイグレーションはありません。")
    return 0


def grant_app_identity(config: PostgresConfig, name: str, token: str) -> None:
    """マネージド ID を PostgreSQL のロールにし、アプリのロールを与える(冪等)。"""
    from psycopg import sql

    if not name or len(name) > 63 or any(ch in name for ch in "\"';\\"):
        raise ValueError("マネージド ID の名前が不正です")
    common = {"host": config.host, "port": config.port, "user": config.user, "sslmode": config.sslmode, "password": token}
    # Entra のプリンシパルは、postgres データベースで作る
    with psycopg.connect(dbname="postgres", autocommit=True, **common) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
            print(f"ロールは作成済み: {name}")
        else:
            conn.execute("SELECT * FROM pgaadauth_create_principal(%s, false, false)", (name,))
            print(f"ロールを作成: {name}")
    with psycopg.connect(dbname=config.database, autocommit=True, **common) as conn:
        role = sql.Identifier(name)
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(config.database), role))
        conn.execute(sql.SQL("GRANT mycompanion_app TO {}").format(role))
        print(f"mycompanion_app を付与: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MyCompanion のスキーマのマイグレーション")
    parser.add_argument("--status", action="store_true", help="適用状況だけを表示する")
    parser.add_argument("--grant-app-identity", metavar="NAME", help="マネージド ID に、アプリのロールを与える")
    args = parser.parse_args()

    try:
        config = PostgresConfig.from_env()
    except ConfigError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2

    if args.grant_app_identity:
        grant_app_identity(config, args.grant_app_identity, default_token_provider()())
        return 0

    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        sslmode=config.sslmode,
        password=default_token_provider()(),
    ) as conn:
        return run(conn, status_only=args.status)


if __name__ == "__main__":
    sys.exit(main())
