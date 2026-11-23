"""実際の Azure(PostgreSQL + pgvector・Azure OpenAI)で、トピック別の時系列記憶を通しで動かすテスト。

`.env` を読み込み、`az login` 済みで、マイグレーション(0011 まで)と開発用データを適用済みであること。
既定では実行しない。費用が少し発生する。

    uv run pytest -m azure tests/live/test_topics_live.py
"""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from mycompanion.config import AzureOpenAIConfig, CosmosConfig, EngineSettings, PostgresConfig
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.postgres import Database, Scope
from mycompanion.db.stores import PgCosmosStores
from mycompanion.db.topic_store import PgTopicStore
from mycompanion.llm.azure_openai import AzureOpenAILLMClient
from mycompanion.memory import okf
from mycompanion.memory.models import Memory
from mycompanion.memory.service import MemoryManager
from mycompanion.memory.topics import MovedMemory

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
    mstore, tstore = PgMemoryStore(database, settings), PgTopicStore(database, settings)
    manager = MemoryManager(mstore, llm, settings, log=stores, topic_store=tstore)
    user_id = await database.resolve_user("test", f"topics-live-{uuid.uuid4()}")
    try:
        yield manager, mstore, tstore, database, llm, user_id
    finally:
        for store in cosmos.all():
            await store.delete_all_for_user(user_id)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (user_id,))
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))  # トピックも、連鎖で消える
            await conn.execute("SET ROLE mycompanion_app")
        await llm.close()
        await cosmos_handle.close()
        await database.close()


async def _memory(mstore, llm, user_id, text: str, told: datetime, character_id: str = CHARACTER, **fields) -> Memory:
    fields.setdefault("importance", 0.5)
    memory = Memory(
        memory_id=uuid.uuid4(), tier="medium", kind="episodic", summary=text, topics=["猫"],
        embedding=(await llm.embed([text]))[0], source_day=told.date(), created_at=told, **fields,
    )  # fmt: skip
    return await mstore.add(user_id, character_id, memory)


async def test_topic_timeline_end_to_end(env):
    manager, mstore, tstore, database, llm, user_id = env
    topics = manager.topics
    start = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)  # 日本時間 10/3(土)正午
    texts = [
        "ユーザーは、保護猫を家族に迎えた。名前はタマ。",
        "ユーザーは、昨日からタマが何も食べなくて、動物病院に連れていった。",
        "ユーザーの猫のタマは、すっかり元気になって、走り回っている。",
    ]
    memories = []
    for offset, text in enumerate(texts):
        told = start + timedelta(days=offset * 7)
        memory = await _memory(mstore, llm, user_id, text, told)
        memories.append(memory)
        counts = await topics.assign_day(user_id, CHARACTER, [MovedMemory(memory, told)], told.date(), told)
        assert counts["entries"] >= 1, counts

    docs = await tstore.list(user_id, CHARACTER)
    tama = [d for d in docs if "タマ" in d.title]
    assert tama, [d.title for d in docs]
    main = max(tama, key=lambda d: len(d.entries))
    # 3日にわたって話に出たので、確立している。文書は、保存したものを読み直しても、検査に通る
    assert main.established and len(main.entries) >= 3
    okf.validate(main)
    assert main.markdown().startswith("---\ntype: Topic ")
    # 「昨日から」は、話した日(10/10)の前日として、コードで正規化される
    entries = [e for d in tama for e in d.entries if e.expression and "昨日" in e.expression]
    assert entries and all(e.occurred.start.isoformat() == "2026-10-09" for e in entries)

    # 会話の話題に合えば、経過を渡す。別のキャラクタからは、見えない
    query = (await llm.embed(["タマ、最近どうしてる?"]))[0]
    timeline = await topics.timeline_for(user_id, CHARACTER, "タマ、最近どうしてる?", query, set())
    assert timeline is not None and timeline.lines
    assert await tstore.list(user_id, OTHER) == []
    assert await topics.timeline_for(user_id, OTHER, "タマ、最近どうしてる?", query, set()) is None
    async with database.scoped_transaction(Scope(user_id, OTHER)) as conn:
        for table in ("topic_doc", "topic_entry", "topic_doc_source"):
            assert await (await conn.execute(f"SELECT count(*) FROM {table}")).fetchone() == (0,)

    # 記憶の削除は、確立したトピックの項目にも伝播する
    before = len(main.entries)
    assert await manager.forget(user_id, CHARACTER, memories[1].memory_id, datetime.now(UTC))
    after = await tstore.get(user_id, CHARACTER, main.doc_id)
    assert after is not None and len(after.entries) < before
    assert okf.source_key(memories[1].memory_id) not in after.markdown()
    assert await tstore.revisions(user_id, CHARACTER, main.doc_id) == []

    # トピックの削除 = アーカイブ。概要だけが残り、会話の経路からは読めない
    archive = await topics.archive(user_id, CHARACTER, after.short_id, datetime.now(UTC))
    assert archive is not None and archive.summary and archive.summary_state == "ok"
    assert await tstore.get(user_id, CHARACTER, main.doc_id) is None
    assert await topics.timeline_for(user_id, CHARACTER, "タマ、最近どうしてる?", query, set()) is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with database.scoped_transaction(Scope(user_id, CHARACTER)) as conn:
            await conn.execute("SELECT * FROM topic_archive")  # アプリのロールには、SELECT がない
    assert [a.title for a in await tstore.archives(user_id, CHARACTER)] == [archive.title]
    assert await tstore.archives(user_id, OTHER) == []  # 関数も、現在のスコープの行だけを返す
    # このトピックにだけ属していた記憶は、想起の対象から外れる
    kept = await mstore.get(user_id, CHARACTER, memories[0].memory_id)
    if all(s.memory_id != memories[0].memory_id for d in await tstore.list(user_id, CHARACTER) for s in d.sources):
        assert kept is not None and kept.recall_excluded == "topic_archived"

    # 根拠の記憶を消すと、概要は取り下げられる。完全な削除もできる
    await manager.forget(user_id, CHARACTER, memories[2].memory_id, datetime.now(UTC))
    (withdrawn,) = await tstore.archives(user_id, CHARACTER)
    assert (withdrawn.summary, withdrawn.summary_state) == ("", "withdrawn")
    assert await topics.purge(user_id, CHARACTER, withdrawn.short_id) is True
    assert await tstore.archives(user_id, CHARACTER) == []


async def test_documents_cannot_cite_memories_of_another_character(env):
    # 別のキャラクタの記憶を根拠にした文書は、データベースが拒否する(複合外部キー)
    _, mstore, tstore, _, llm, user_id = env
    told = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
    foreign = await _memory(mstore, llm, user_id, "ユーザーは犬のポチと散歩した。", told, character_id=OTHER)
    doc = okf.TopicDoc(
        doc_id=uuid.uuid4(), path=okf.new_path("entity"), okf_type=okf.TYPE_ENTITY, title="ポチ",
        generated_by="process:test", generated_at=told, created_at=told, updated_at=told,
        sources=[okf.Source(okf.source_key(foreign.memory_id), foreign.memory_id, told.date(), told)],
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await tstore.save(user_id, CHARACTER, doc, "creation")
    assert await tstore.list(user_id, CHARACTER) == []


async def test_followup_offer_and_topic_recall_on_postgres(env):
    # 第2段階: 予定の「その後」の候補、キャラクタ起点の想起(保留 → 確定)、活性度が、PostgreSQL の上で動く
    manager, mstore, tstore, _, llm, user_id = env
    topics = manager.topics
    told = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
    plan = await _memory(mstore, llm, user_id, "ユーザーは、来週の金曜に転職の面接があると話した。", told, importance=0.9)
    counts = await topics.assign_day(user_id, CHARACTER, [MovedMemory(plan, told)], told.date(), told)
    assert counts["entries"] >= 1 and counts["established"] >= 1, counts
    docs = [d for d in await tstore.list(user_id, CHARACTER) if d.stale_after is not None]
    assert docs, [(d.title, [e.kind for e in d.entries]) for d in await tstore.list(user_id, CHARACTER)]
    doc = docs[0]
    assert doc.stale_after == datetime(2026, 10, 10, tzinfo=UTC)

    now, session_id = datetime(2026, 10, 12, 3, 0, tzinfo=UTC), uuid.uuid4()
    query = (await llm.embed(["うん"]))[0]
    offer = await topics.offer(user_id, CHARACTER, query, now, session_id)
    assert offer is not None and offer.kind == "followup" and offer.doc_id == doc.doc_id
    await topics.record_offer_used(user_id, CHARACTER, offer, session_id, "turn-1", now.date(), now)
    saved = await tstore.get(user_id, CHARACTER, doc.doc_id)
    assert saved.followup_keys == [offer.entry_key] and saved.last_followup_at == now and saved.version == doc.version
    (event,) = await tstore.recalls(user_id, CHARACTER, doc.doc_id)
    assert (event.trigger, event.source, event.status) == ("character", "followup", "pending")
    assert await topics.character_mentions(user_id, CHARACTER, session_id) == 1
    assert await tstore.recalls(user_id, OTHER) == []

    # ユーザーが乗ってくれば、確定する。同じ予定は、もう聞かない
    reply = (await llm.embed(["転職の面接、思ったよりうまく話せたよ"]))[0]
    assert (await topics.resolve_pending(user_id, CHARACTER, reply, 0.5, now.date(), now))["confirmed"] == 1
    assert (await tstore.get(user_id, CHARACTER, doc.doc_id)).activation > 0
    again = await topics.offer(user_id, CHARACTER, query, now + timedelta(days=8), uuid.uuid4())
    assert again is None or again.kind == "topic"


async def test_relations_and_association_setting_on_postgres(env):
    # 第3段階: 関係は文書(frontmatter の mc_relations)に保存され、読み直せる。連想の設定は、ユーザーごとに保存される
    manager, mstore, tstore, database, llm, user_id = env
    told = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)
    memory = await _memory(mstore, llm, user_id, "ユーザーは、猫のタマが体調を崩して通院したと話した。", told, importance=0.9)
    key = okf.source_key(memory.memory_id)

    def doc(kind: str, title: str) -> okf.TopicDoc:
        entry = okf.Entry("t1", "event", f"{title}のこと", okf.Occurred(None, None, "unknown"), told.date(), [key])
        return okf.TopicDoc(
            doc_id=uuid.uuid4(), path=okf.new_path(kind), okf_type=okf.TYPES[kind], title=title, status="stable",
            lifecycle="active", generated_by="process:test", generated_at=told, created_at=told, updated_at=told,
            sources=[okf.Source(key, memory.memory_id, told.date(), told)], entries=[entry], next_entry_no=2,
        )  # fmt: skip

    cat, illness = doc("entity", "タマ"), doc("thread", "タマの体調不良")
    illness.relations = [okf.Relation("r1", cat.path, "involves", "タマ", "subject", "stated", [key])]
    for item in (cat, illness):
        okf.validate(item)
        await tstore.save(user_id, CHARACTER, item, "creation")
    loaded = await tstore.get(user_id, CHARACTER, illness.doc_id)
    assert loaded.relations == illness.relations and "# 関係" in loaded.body()
    query = (await llm.embed(["タマの体調"]))[0]
    timeline = await manager.topics.timeline_for(user_id, CHARACTER, "タマの体調不良のことだけど", query, set())
    assert timeline is not None and any("タマ" in line for line in timeline.related)
    # 相手のトピックを削除(アーカイブ)すると、関係も消える
    await manager.topics.archive(user_id, CHARACTER, cat.short_id, datetime.now(UTC))
    assert (await tstore.get(user_id, CHARACTER, illness.doc_id)).relations == []

    stores = PgCosmosStores(database, None, EngineSettings())  # type: ignore[arg-type]
    assert (await stores.load_user_context(user_id)).association_enabled is True
    changed = await stores.update_user_settings(user_id, {"association_enabled": False})
    assert changed.association_enabled is False
