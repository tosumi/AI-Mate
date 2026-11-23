"""実際の Azure(PostgreSQL・Cosmos DB)で、自発発話の保存と、返事のつながりを確かめるテスト(§15.1 の M8)。

LLM は模擬のものを使う(保存先の動作だけを確かめる)。マイグレーション 0018 を適用済みであること。

    uv run pytest -m azure tests/live/test_proactive_live.py
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import CosmosConfig, EngineSettings, PostgresConfig
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.postgres import Database
from mycompanion.db.stores import PgCosmosStores
from mycompanion.engine.engine import Engine
from mycompanion.engine.stores import TurnRecord
from mycompanion.llm.mock import MockLLMClient

pytestmark = pytest.mark.azure

CHARACTER = "sample_kuchibeta"


@pytest.fixture
async def env():
    settings = EngineSettings()
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    cosmos_handle, cosmos = open_cosmos(
        CosmosConfig.from_env(), settings.turns_retention_days, settings.security_events_retention_days
    )
    stores = PgCosmosStores(database, cosmos, settings)
    user_id = await database.resolve_user("test", f"proactive-live-{uuid.uuid4()}")
    try:
        yield stores, PgMemoryStore(database, settings), Engine(MockLLMClient(), stores, settings), user_id
    finally:
        for store in cosmos.all():
            await store.delete_all_for_user(user_id)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await cosmos_handle.close()
        await database.close()


def _record(now: datetime) -> TurnRecord:
    return TurnRecord(
        turn_id=str(uuid.uuid4()), session_id=uuid.uuid4(), ts=now, user_input="", draft_text="そういえば、面接どうだった?",
        filtered_text="そういえば、面接どうだった?", response_mode="proactive", filter_mode="off", character_version=1,
        reply_text="そういえば、面接どうだった?", compose_mode="single", reply_model="mock",
        reply_prompt_version="mock+proactive-v1",
    )  # fmt: skip


async def test_setting_snapshot_record_and_reply(env):
    stores, memory_store, engine, user_id = env
    # 既定はオフ。ユーザーが許可すると、オンになる
    assert (await stores.load_user_context(user_id)).proactive_enabled is False
    assert (await stores.update_user_settings(user_id, {"proactive_enabled": True})).proactive_enabled is True

    now = datetime.now(UTC)
    empty = await stores.proactive_snapshot(user_id, CHARACTER, now)
    assert (empty.last_activity_at, empty.unanswered, empty.sent_today, empty.session_active) == (None, 0, 0, False)

    # 自発発話: 新しいセッションの最初のターン。返事のない数と、1日の回数が増える
    saved = await stores.record_proactive(user_id, CHARACTER, _record(now), now, now + timedelta(hours=48))
    assert saved.turn_no == 1
    snap = await stores.proactive_snapshot(user_id, CHARACTER, now)
    assert (snap.unanswered, snap.sent_today, snap.session_active) == (1, 1, True)
    assert abs((snap.last_activity_at - now).total_seconds()) < 1
    assert await stores.recent_turns(user_id, CHARACTER, saved.session_id, 10) == [
        ("assistant", "そういえば、面接どうだった?")
    ]

    # 返事を待つあいだは、無入力でも閉じない(スケジューラの一覧にも出ない)
    idle = await memory_store.idle_sessions(now + timedelta(hours=1))
    assert all(s[2] != saved.session_id for s in idle)
    later = now + timedelta(hours=3)
    session = await stores.get_or_create_session(user_id, CHARACTER, "cli", later)
    assert session.session_id == saved.session_id and not session.is_new

    # ユーザーの返事は、同じセッションの続き。返事のない数は 0 に戻る
    result = await engine.handle_turn(
        user_id, TurnInput(channel="cli", character_id=CHARACTER, text="うまくいったよ"), now=later
    )
    assert (result.session_id, result.turn_no) == (saved.session_id, 2)
    assert (await stores.proactive_snapshot(user_id, CHARACTER, later)).unanswered == 0


async def test_candidates_list_only_allowed_users_without_an_active_session(env):
    stores, memory_store, engine, user_id = env
    now = datetime.now(UTC)
    await engine.handle_turn(user_id, TurnInput(channel="cli", character_id=CHARACTER, text="こんにちは"), now=now)
    await stores.close_session(user_id, CHARACTER, "user_exit", now)
    before = now + timedelta(minutes=1)
    assert (user_id, CHARACTER) not in {(u, c) for u, c, _ in await memory_store.proactive_candidates(before)}
    await stores.update_user_settings(user_id, {"proactive_enabled": True})
    found = {(u, c): tz for u, c, tz in await memory_store.proactive_candidates(before)}
    assert found.get((user_id, CHARACTER)) == "Asia/Tokyo"
    # 最後の会話が新しければ、まだ候補にしない
    assert (user_id, CHARACTER) not in {(u, c) for u, c, _ in await memory_store.proactive_candidates(now - timedelta(minutes=1))}
