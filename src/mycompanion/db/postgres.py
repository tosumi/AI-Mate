"""PostgreSQL への接続と、行レベルセキュリティ(RLS)のスコープ設定(本体仕様書 §13.4.6)。

- 認証は Entra ID のトークンで行う(パスワードを使わない。§11.5.9)。
- アプリのクエリは、必ず `scoped_transaction` の中で実行する。
  トランザクションごとに `app.user_id` と `app.character_id` を設定し、
  他のユーザー・他のキャラクタの行に、データベース側でアクセスできないようにする。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import psycopg
from psycopg_pool import AsyncConnectionPool

from mycompanion.config import PostgresConfig

# Azure Database for PostgreSQL の Entra 認証で使うスコープ
POSTGRES_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# トークンを返す関数(テストで差し替えられるようにする)
TokenProvider = Callable[[], str]


def default_token_provider() -> TokenProvider:
    """マネージド ID(クラウド)または `az login`(ローカル)の資格情報でトークンを取る。"""
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()

    def _token() -> str:
        return credential.get_token(POSTGRES_AAD_SCOPE).token

    return _token


@dataclass(frozen=True)
class Scope:
    """データアクセスの範囲。キャラクタをまたぐテーブルだけを扱うときは character_id を None にする。"""

    user_id: uuid.UUID
    character_id: str | None = None


class Database:
    """接続プールと、スコープつきのトランザクション。"""

    def __init__(self, config: PostgresConfig, token_provider: TokenProvider | None = None) -> None:
        self._config = config
        self._token_provider = token_provider or default_token_provider()
        self._pool: AsyncConnectionPool | None = None

    def _conninfo(self) -> str:
        # パスワード(トークン)は conninfo に入れず、接続のたびに kwargs で渡す
        c = self._config
        return psycopg.conninfo.make_conninfo(
            host=c.host, port=c.port, dbname=c.database, user=c.user, sslmode=c.sslmode
        )

    async def open(self, min_size: int = 1, max_size: int = 10) -> None:
        self._pool = AsyncConnectionPool(
            self._conninfo(),
            min_size=min_size,
            max_size=max_size,
            max_lifetime=30 * 60,  # トークンの有効期限より短い間隔で接続を作り直す
            connection_class=_TokenConnection.bind(self._token_provider),
            configure=_use_app_role,
            open=False,
        )
        # 最初の接続ができるまで待つ。つながらない場合は、ここで早めにエラーにする
        await self._pool.open(wait=True, timeout=20)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Database.open() が呼ばれていません")
        return self._pool

    async def ping(self) -> bool:
        """疎通確認(ヘルスチェック用)。"""
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            return row == (1,)

    async def resolve_user(self, idp: str, subject: str) -> uuid.UUID:
        """トークンのサブジェクトから内部の user_id を引く(初回は作成する。§13.4.2)。"""
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT resolve_user(%s, %s)", (idp, subject))).fetchone()
            assert row is not None
            return row[0]

    @asynccontextmanager
    async def scoped_transaction(self, scope: Scope) -> AsyncIterator[psycopg.AsyncConnection]:
        """スコープを設定したトランザクション。設定はトランザクションの終了で消える。"""
        async with self.pool.connection() as conn, conn.transaction():
            await apply_scope(conn, scope)
            yield conn


# アプリが使うロール。RLS を迂回できない(マイグレーション 0001 で作成)
APP_ROLE = "mycompanion_app"


async def _use_app_role(conn: psycopg.AsyncConnection) -> None:
    """接続を、RLS の対象になるアプリ用のロールに切り替える。

    ログインしたロール(マネージド ID や開発者)がテーブルの所有者・管理者であっても、
    アプリのクエリは必ず RLS の下で実行する。切り替えられない場合は、接続を使わせない。
    """
    await conn.execute(f"SET ROLE {APP_ROLE}")
    await conn.commit()


async def apply_scope(conn: psycopg.AsyncConnection, scope: Scope) -> None:
    """RLS が参照する設定を、現在のトランザクションに限って設定する。"""
    # 第3引数 true: トランザクションの終了で元に戻る(接続プールで他のリクエストに漏れない)
    await conn.execute("SELECT set_config('app.user_id', %s, true)", (str(scope.user_id),))
    await conn.execute(
        "SELECT set_config('app.character_id', %s, true)", (scope.character_id or "",)
    )


async def lock_user_character(conn: psycopg.AsyncConnection, scope: Scope) -> None:
    """ターンの状態更新を (user_id, character_id) 単位で直列化するアドバイザリロック(§11.5.4)。

    トランザクションの終了で自動的に解放される。
    """
    if scope.character_id is None:
        raise ValueError("character_id が必要です")
    key = f"{scope.user_id}:{scope.character_id}"
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


class _TokenConnection(psycopg.AsyncConnection):
    """接続のたびに Entra ID のトークンをパスワードとして渡す接続クラス。"""

    _token_provider: TokenProvider

    @classmethod
    def bind(cls, token_provider: TokenProvider) -> type[_TokenConnection]:
        return type("_BoundTokenConnection", (cls,), {"_token_provider": staticmethod(token_provider)})

    @classmethod
    async def connect(cls, conninfo: str = "", **kwargs: object) -> _TokenConnection:  # type: ignore[override]
        kwargs["password"] = cls._token_provider()
        return await super().connect(conninfo, **kwargs)  # type: ignore[arg-type,return-value]
