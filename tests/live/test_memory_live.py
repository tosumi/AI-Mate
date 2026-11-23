"""実際の Azure(PostgreSQL + pgvector・Cosmos DB・Azure OpenAI)で、記憶を通しで動かすテスト。

`.env` を読み込み、`az login` 済みで、マイグレーション(0004 まで)と開発用データを適用済みであること。
既定では実行しない。費用が少し発生する。

    uv run pytest -m azure tests/live/test_memory_live.py
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from mycompanion.channel.types import TurnInput
from mycompanion.config import AzureOpenAIConfig, CosmosConfig, EngineSettings, PostgresConfig
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.postgres import Database, Scope
from mycompanion.db.stores import PgCosmosStores
from mycompanion.engine.engine import Engine
from mycompanion.engine.memory_ops import MemoryOps
from mycompanion.llm.azure_openai import AzureOpenAILLMClient
from mycompanion.memory import scoring
from mycompanion.memory.models import Memory, RecallEvent
from mycompanion.memory.service import MemoryManager

pytestmark = pytest.mark.azure

CHARACTER = "sample_hitonatsukoi"
OTHER = "sample_kamoku"


@pytest.fixture
async def env():
    settings = EngineSettings()
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    cosmos_handle, cosmos = open_cosmos(
        CosmosConfig.from_env(), settings.turns_retention_days, settings.security_events_retention_days
    )
    llm = AzureOpenAILLMClient(AzureOpenAIConfig.from_env(), settings)
    stores = PgCosmosStores(database, cosmos, settings)
    mstore = PgMemoryStore(database, settings)
    manager = MemoryManager(mstore, llm, settings, log=stores)
    engine = Engine(llm, stores, settings, manager)
    user_id = await database.resolve_user("test", f"memory-live-{uuid.uuid4()}")
    try:
        yield engine, manager, mstore, stores, database, llm, user_id
    finally:
        await engine.drain()
        for store in cosmos.all():
            await store.delete_all_for_user(user_id)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (user_id,))
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await llm.close()
        await cosmos_handle.close()
        await database.close()


def _turn(text: str, character_id: str = CHARACTER) -> TurnInput:
    return TurnInput(channel="cli", character_id=character_id, text=text)


async def test_store_roundtrip_search_and_isolation(env):
    _, _, mstore, _, database, llm, user_id = env
    now = datetime.now(UTC)
    texts = ["ユーザーは猫のタマを飼っている", "ユーザーは来月、札幌に引っ越す予定だ"]
    vectors = await llm.embed([*texts, "うちの猫が最近よく寝ている"])
    memories = [
        Memory(
            memory_id=uuid.uuid4(), tier="medium", kind="semantic", summary=text, topics=["猫"], importance=0.6,
            emotion_vad={"valence": 0.4, "arousal": 0.1, "dominance": 0.0}, source_day=date(2026, 11, 10),
            source_event_ids=["t1"], embedding=vector, created_at=now, expires_at=now + timedelta(days=90),
        )
        for text, vector in zip(texts, vectors[:2], strict=True)
    ]
    for memory in memories:
        await mstore.add(user_id, CHARACTER, memory)

    loaded = await mstore.get(user_id, CHARACTER, memories[0].memory_id)
    assert loaded.summary == texts[0] and loaded.topics == ["猫"] and len(loaded.embedding) == 1536
    assert loaded.emotion_vad["valence"] == 0.4 and loaded.source_day == date(2026, 11, 10)

    found = await mstore.candidates(user_id, CHARACTER, vectors[2], "medium", 5)
    assert found[0][0].memory_id == memories[0].memory_id and found[0][1] > found[1][1]
    assert found[0][1] >= scoring.MIN_SIMILARITY  # 関係のある記憶は、しきい値を超える
    # 別のキャラクタからは、見えない。スコープなしでは、何も読めない(RLS)
    assert await mstore.candidates(user_id, OTHER, vectors[2], "medium", 5) == []
    assert await mstore.get(user_id, OTHER, memories[0].memory_id) is None
    async with database.scoped_transaction(Scope(uuid.uuid4(), CHARACTER)) as conn:
        assert await (await conn.execute("SELECT count(*) FROM memory")).fetchone() == (0,)

    await mstore.update(user_id, CHARACTER, memories[0].memory_id, muted=True, topics=["猫", "タマ"], version=2)
    event = RecallEvent(memories[0].memory_id, date(2026, 11, 16), "user", "search", 1.0, ts=now, turn_id="t9")
    assert await mstore.add_recall(user_id, CHARACTER, event) is True
    assert await mstore.add_recall(user_id, CHARACTER, event) is False  # 同じ日・同じ起点は1回
    assert len(await mstore.recalls(user_id, CHARACTER, memories[0].memory_id)) == 1

    targets = [t for t in await mstore.all_scopes() if t.user_id == user_id]
    assert [(t.character_id, t.timezone) for t in targets] == [(CHARACTER, "Asia/Tokyo")]
    assert await mstore.soft_delete(user_id, CHARACTER, [memories[0].memory_id], now) == 1
    assert await mstore.get(user_id, CHARACTER, memories[0].memory_id) is None
    gone = await mstore.get(user_id, CHARACTER, memories[0].memory_id, include_deleted=True)
    assert gone.summary == "" and gone.embedding is None  # 削除した記憶の本文は、残さない


async def test_conversation_is_remembered_consolidated_and_forgotten(env):
    engine, manager, mstore, stores, _, _, user_id = env
    ops = MemoryOps(stores, manager, EngineSettings())

    first = await engine.handle_turn(user_id, _turn("うちでは、タマっていう三毛猫を飼ってるんだ。もう5年になるよ"))
    await engine.handle_turn(user_id, _turn("今日はタマが、朝からずっと窓のそばで日向ぼっこしてた"))
    assert first.state["memory"]["retrieved"] == []
    assert await engine.close_session(user_id, CHARACTER) is True

    shorts = await mstore.list(user_id, CHARACTER, tier="short")
    assert shorts and all(m.embedding and m.source_event_ids for m in shorts)
    assert any("タマ" in m.summary or "猫" in m.summary for m in shorts)
    assert await mstore.list(user_id, OTHER) == []

    # 次のセッション: 前の会話の記憶が、検索で選ばれて、下書きに渡る
    again = await engine.handle_turn(user_id, _turn("うちの猫の名前、覚えてる?"))
    assert again.state["memory"]["retrieved"], "前のセッションの記憶が検索されること"
    assert "タマ" in again.output.actions[-1].text
    assert again.state["memory"]["used"], "応答で使った記憶の番号が返ること"
    # 別のキャラクタは、この記憶を知らない(§13.4.6)
    other = await engine.handle_turn(user_id, _turn("うちの猫の名前、覚えてる?", OTHER))
    assert other.state["memory"]["retrieved"] == [] and "タマ" not in other.output.actions[-1].text
    await engine.close_session(user_id, CHARACTER)
    await engine.close_session(user_id, OTHER)

    # 日次整理: 短期 → 中期。冪等
    runs = await ops.consolidate(user_id, CHARACTER)
    assert [r["status"] for r in runs] == ["success"] and runs[0]["counts"]["written"] >= 1
    assert await mstore.list(user_id, CHARACTER, tier="short") == []
    kept = [m for t in ("medium", "long") for m in await mstore.list(user_id, CHARACTER, tier=t)]
    assert kept and any("タマ" in m.summary for m in kept)
    assert await ops.consolidate(user_id, CHARACTER) == []
    status = await ops.status(user_id, CHARACTER)
    assert status["last_daily"]["status"] == "success" and status["counts"]["short"] == 0

    # 月次の概要(今月分を、手動で作る)
    period = scoring.local_day(datetime.now(UTC), "Asia/Tokyo").strftime("%Y-%m")
    monthly = await ops.consolidate(user_id, CHARACTER, monthly=True, month=period)
    assert monthly[0]["status"] == "success"
    (summary,) = await mstore.list(user_id, CHARACTER, kind="monthly_summary")
    assert summary.period == period and summary.summary

    # 削除: 記憶、根拠のターン、月次の概要の該当部分に伝播する
    listed = await ops.list(user_id, CHARACTER)
    target = next(item for item in listed if "タマ" in item["summary"] and item["kind"] != "monthly_summary")
    assert (await ops.forget(user_id, CHARACTER, target["id"][:8]))["deleted"] is True
    assert all(item["id"] != target["id"] for item in await ops.list(user_id, CHARACTER))
    emotions = await ops.emotion_list(user_id, CHARACTER, days=3)
    assert emotions["days"] and emotions["days"][0]["count"] >= 1
