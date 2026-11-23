"""実際の PostgreSQL に接続して、行レベルセキュリティを検証する(本体仕様書 §13.4.6、§14)。

Azure の dev 用 DB が必要(`.env` を読み込み、`az login` 済みであること)。既定では実行しない。

    uv run pytest -m azure tests/live
"""

import uuid

import psycopg
import pytest

from mycompanion.config import PostgresConfig
from mycompanion.db.postgres import Database, Scope

pytestmark = pytest.mark.azure


@pytest.fixture
async def db():
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    yield database
    await database.close()


async def _new_user(db: Database) -> uuid.UUID:
    return await db.resolve_user("test", f"rls-test-{uuid.uuid4()}")


async def _count(db: Database, scope: Scope) -> int:
    async with db.scoped_transaction(scope) as conn:
        row = await (await conn.execute("SELECT count(*) FROM memory")).fetchone()
        return row[0]


async def test_memory_is_isolated_by_user_and_character(db):
    user_a, user_b = await _new_user(db), await _new_user(db)
    try:
        async with db.scoped_transaction(Scope(user_a, "char_1")) as conn:
            await conn.execute(
                "INSERT INTO memory (user_id, character_id, tier, kind, summary) "
                "VALUES (%s, 'char_1', 'short', 'episodic', 'テスト用の記憶')",
                (user_a,),
            )
        assert await _count(db, Scope(user_a, "char_1")) == 1
        # 同じユーザーの別のキャラクタ、別のユーザー、スコープなしでは見えない
        assert await _count(db, Scope(user_a, "char_2")) == 0
        assert await _count(db, Scope(user_b, "char_1")) == 0
        assert await _count(db, Scope(user_a, None)) == 0
    finally:
        await _cleanup(db, user_a, user_b)


async def test_cannot_write_into_another_scope(db):
    user_a, user_b = await _new_user(db), await _new_user(db)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.scoped_transaction(Scope(user_a, "char_1")) as conn:
                # 他のユーザーの行として書き込もうとすると、データベースが拒否する
                await conn.execute(
                    "INSERT INTO memory (user_id, character_id, tier, kind, summary) "
                    "VALUES (%s, 'char_1', 'short', 'episodic', '偽装')",
                    (user_b,),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.scoped_transaction(Scope(user_a, "char_1")) as conn:
                await conn.execute(
                    "INSERT INTO memory (user_id, character_id, tier, kind, summary) "
                    "VALUES (%s, 'char_2', 'short', 'episodic', '別キャラクタへの書き込み')",
                    (user_a,),
                )
    finally:
        await _cleanup(db, user_a, user_b)


async def test_app_role_is_enforced_and_scope_does_not_leak(db):
    async with db.pool.connection() as conn:
        row = await (await conn.execute("SELECT current_user, app_user_id()")).fetchone()
        # 接続はアプリ用のロールで、前のトランザクションのスコープは残っていない
        assert row == ("mycompanion_app", None)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("SELECT * FROM audit_logs LIMIT 1")  # 監査ログは追記のみ


async def _cleanup(db: Database, *user_ids: uuid.UUID) -> None:
    """テスト用のユーザーを消す。users の削除はアプリのロールではできないので、管理者に戻して行う。"""
    async with db.pool.connection() as conn:
        await conn.execute("RESET ROLE")
        for user_id in user_ids:
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
        await conn.execute("SET ROLE mycompanion_app")
