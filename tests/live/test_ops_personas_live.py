"""実際の PostgreSQL で、キャラクタの提出・公開の関数(0022)を確かめる(M10 の手順4)。

`.env` を読み込み、`az login` 済みで、マイグレーション 0022 を適用済みであること。既定では実行しない。

    uv run --env-file .env pytest -m azure tests/live/test_ops_personas_live.py
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mycompanion.config import PostgresConfig
from mycompanion.db.ops_store import PgPersonaStore
from mycompanion.db.postgres import Database
from mycompanion.ops.personas import PersonaError, PersonaPublisher
from mycompanion.persona import io as pio

pytestmark = pytest.mark.azure


@pytest.fixture
async def env():
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    character_id = f"ops_live_{uuid.uuid4().hex[:10]}"
    try:
        yield database, character_id
    finally:
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM character_reviews WHERE character_id = %s", (character_id,))
            await conn.execute("DELETE FROM characters WHERE character_id = %s", (character_id,))
            await conn.execute("DELETE FROM persona_submissions WHERE character_id = %s", (character_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await database.close()


def _definition(character_id: str, version: str) -> dict:
    raw = pio.load_raw(pio.persona_path("sample_kamoku", Path("personas")))
    return {**raw, "character_id": character_id, "version": version}


async def test_submit_publish_rollback_on_postgres(env):
    database, character_id = env
    store = PgPersonaStore(database)
    publisher = PersonaPublisher(store, Path("personas"))
    now = datetime.now(UTC)

    first = await publisher.submit(_definition(character_id, "1.0.0"), "designer-oid", now)
    sid = uuid.UUID(first["submission_id"])
    # 保存先の関数でも、提出した本人の承認を止める(API の確認を通さずに呼んでも)
    with pytest.raises(PersonaError) as error:
        await store.publish(sid, "designer-oid", "ok", now)
    assert error.value.kind == "forbidden"
    assert await publisher.approve(sid, "approver-oid", "確認済み", now) == {
        "published_version": 1, "character_id": character_id, "version_label": "1.0.0",
    }  # fmt: skip

    # 公開した行は、会話の側(アプリのロールの RLS)から読める
    async with database.pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT name, status, traits_json, version_label FROM characters WHERE character_id = %s", (character_id,)
        )).fetchone()  # fmt: skip
    assert row and row[1] == "published" and "talkativeness" in row[2] and row[3] == "1.0.0"

    second = uuid.UUID((await publisher.submit(_definition(character_id, "1.1.0"), "designer-oid", now))["submission_id"])
    assert (await publisher.approve(second, "approver-oid", "ok", now))["published_version"] == 2
    stale = uuid.UUID((await publisher.submit(_definition(character_id, "1.0.1"), "designer-oid", now))["submission_id"])
    with pytest.raises(PersonaError) as error:
        await publisher.approve(stale, "approver-oid", "ok", now)
    assert error.value.kind == "conflict"
    assert await publisher.reject(stale, "approver-oid", "古い版", now) is None

    assert len(await publisher.diff(character_id, 1, 2)) == 0  # 中身は同じ(版の番号だけが違う)
    assert await publisher.rollback(character_id, "1.0.0", "approver-oid", now) == 1
    versions = {v["version"]: v["status"] for v in await store.list_versions(character_id)}
    assert versions == {1: "published", 2: "deprecated"}
    assert await publisher.deprecate(character_id, "approver-oid", now) == 1
    detail = await publisher.detail(sid)
    assert detail["status"] == "approved" and detail["decided_by"] == "approver-oid"
