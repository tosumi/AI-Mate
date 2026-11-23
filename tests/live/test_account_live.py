"""実際の Azure(PostgreSQL・Cosmos DB)で、アカウントの操作と、ペルソナの公開手順を確かめるテスト。

`.env` を読み込み、`az login` 済みで、マイグレーション(0007 まで)を適用済みであること。既定では実行しない。

    uv run pytest -m azure tests/live/test_account_live.py
"""

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from mycompanion.account.service import REQUIRED, AccountOps
from mycompanion.account.store import PgAccountStore
from mycompanion.config import CosmosConfig, EngineSettings, PostgresConfig
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.postgres import Database, Scope, default_token_provider
from mycompanion.db.stores import PgCosmosStores
from mycompanion.memory.models import Memory
from mycompanion.persona import io as pio
from mycompanion.persona import publish as pub
from mycompanion.persona.lint import lint

pytestmark = pytest.mark.azure

CHARACTER = "sample_kamoku"


@pytest.fixture
async def env():
    settings = EngineSettings()
    database = Database(PostgresConfig.from_env())
    await database.open(min_size=1, max_size=2)
    cosmos_handle, cosmos = open_cosmos(
        CosmosConfig.from_env(), settings.turns_retention_days, settings.security_events_retention_days
    )
    mstore = PgMemoryStore(database, settings)
    stores = PgCosmosStores(database, cosmos, settings)
    ops = AccountOps(PgAccountStore(database, cosmos, mstore))
    user_id = await database.resolve_user("test", f"account-live-{uuid.uuid4()}")
    try:
        yield ops, stores, mstore, cosmos, database, user_id
    finally:
        for store in cosmos.all():
            await store.delete_all_for_user(user_id)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (user_id,))
            await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s", (user_id,))
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await cosmos_handle.close()
        await database.close()


async def test_consent_age_export_and_delete(env):
    ops, stores, mstore, cosmos, database, user_id = env
    now = datetime.now(UTC)
    # 年齢に答えるまでは、18歳未満の扱い(保護者の同意の確認も、必須)
    assert set(await ops.missing(user_id)) == {*REQUIRED, "memory", "guardian_confirmed"}
    assert (await ops.declare_age(user_id, True)) == {"is_under_18": False}
    assert (await stores.load_user_context(user_id)).is_under_18 is False
    state = await ops.grant(user_id, [*REQUIRED, "memory"], "cli")
    assert state["missing"] == [] and state["memory"] is True and state["version"] == "1.0-draft"
    await ops.grant(user_id, list(REQUIRED))  # 2回記録しても、重複しない
    state = await ops.grant(user_id, ["sensitive_memory", "romantic_expression"])
    assert state["sensitive_memory"] is True and state["romantic_expression"] is True
    assert (await ops.revoke(user_id, "memory"))["memory"] is False
    assert await ops.missing(user_id) == []  # 記憶の同意は、撤回しても、会話はできる

    # 書き出すデータを用意する(関係値、記憶、会話の原文)
    await stores.set_relationship(user_id, CHARACTER, 5.0, "test", now)
    await mstore.add(
        user_id, CHARACTER,
        Memory(memory_id=uuid.uuid4(), tier="long", kind="semantic", summary="ユーザーは『テスト』と呼ばれたい",
               core_key="user_name", pinned=True, importance=0.9, created_at=now),
    )  # fmt: skip
    await cosmos.turns.put(user_id, CHARACTER, {"sessionId": "s", "turnNo": 1, "ts": now.isoformat(),
                                               "user_input": "こんにちは", "reply_text": "こんにちは。"})  # fmt: skip

    data = await ops.export(user_id)
    assert data["settings"]["is_under_18"] is False and len(data["consents"]) == 8
    record = next(c for c in data["consents"] if c["type"] == "third_party_ai")
    assert len(record["text_hash"]) == 64 and record["channel"] == "cli"
    character = data["characters"][CHARACTER]
    assert character["relationship"] == 5.0 and character["memories"][0]["core"] == "user_name"
    assert character["turns"] == [{"ts": now.isoformat(), "user_input": "こんにちは", "reply": "こんにちは。"}]

    result = await ops.delete(user_id)
    assert result["counts"]["account"] == 1 and result["counts"]["turns"] == 1
    assert await mstore.list(user_id, CHARACTER) == []
    assert [t async for t in cosmos.turns.query(user_id, CHARACTER)] == []
    async with database.pool.connection() as conn:
        await conn.execute("RESET ROLE")
        users = await (await conn.execute("SELECT count(*) FROM users WHERE user_id = %s", (user_id,))).fetchone()
        # 削除の記録と監査ログは残るが、user_id は、元に戻せない別の値に置き換わっている
        left = await (
            await conn.execute("SELECT count(*) FROM audit_logs WHERE target_user_id = %s", (user_id,))
        ).fetchone()
        alias = await (
            await conn.execute(
                "SELECT target_user_id FROM audit_logs WHERE action = 'account_deleted' AND ts >= %s", (now,)
            )
        ).fetchone()
        assert alias is not None
        requests = await (
            await conn.execute("SELECT scope, status FROM deletion_requests WHERE user_id = %s", (alias[0],))
        ).fetchall()
        audit = await (
            await conn.execute("SELECT action FROM audit_logs WHERE target_user_id = %s ORDER BY ts", (alias[0],))
        ).fetchall()
        # このテストが残した行を消す(置き換え後は、user_id では探せないため)
        await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s", (alias[0],))
        await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (alias[0],))
        await conn.execute("SET ROLE mycompanion_app")
    assert users == (0,) and left == (0,) and requests == [("all", "completed")]
    assert [a[0] for a in audit] == ["user_created", "account_deleted"]  # 削除した事実は、監査ログに残る
    # 別のユーザーとして、削除の関数を呼んでも、何も消えない(本人にしか効かない)
    async with database.scoped_transaction(Scope(uuid.uuid4())) as conn:
        assert await (await conn.execute("SELECT delete_my_account()")).fetchone() == (False,)


def test_publish_rollback_and_diff_on_a_temporary_character():
    config = PostgresConfig.from_env()
    character_id = f"tmp_publish_{uuid.uuid4().hex[:8]}"
    raw = pio.load_raw(pio.persona_path(CHARACTER))
    raw["character_id"] = character_id
    report = lint(raw)
    assert report.definition is not None and report.ok
    conn = psycopg.connect(
        host=config.host, port=config.port, dbname=config.database, user=config.user, sslmode=config.sslmode,
        password=default_token_provider()(),
    )  # fmt: skip
    try:
        first = pub.publish(conn, report.definition, "1.0.0", "レビュー担当", "テスト", report)
        assert (first.version, first.label) == (1, "1.0.0")
        with pytest.raises(pub.PublishError):
            pub.publish(conn, report.definition, "1.0.0", "レビュー担当", None, report)  # 同じ版は、公開できない
        raw["temperament"]["reactivity"] = 0.9
        changed = lint(raw)
        second = pub.publish(conn, changed.definition, "1.1.0", "レビュー担当", None, changed)
        assert second.version == 2
        assert [(v.label, v.status) for v in pub.versions(conn, character_id)] == [
            ("1.0.0", "published"), ("1.1.0", "published"),
        ]  # fmt: skip
        assert ("temperament.reactivity", 0.3, 0.9) in pub.diff(conn, character_id, "1.0.0", "1.1.0")

        pub.rollback(conn, character_id, "1.0.0")
        assert [(v.label, v.status) for v in pub.versions(conn, character_id)] == [
            ("1.0.0", "published"), ("1.1.0", "deprecated"),
        ]  # fmt: skip
        with pytest.raises(pub.PublishError):
            pub.rollback(conn, character_id, "9.9.9")
        assert pub.deprecate(conn, character_id) == 1
        reviews = conn.execute(
            "SELECT decision FROM character_reviews WHERE character_id = %s ORDER BY reviewed_at", (character_id,)
        ).fetchall()
        assert [r[0] for r in reviews] == ["approved", "approved", "rollback"]
    finally:
        conn.rollback()  # 途中で失敗していたら、取り消す
        conn.execute("DELETE FROM character_reviews WHERE character_id = %s", (character_id,))
        conn.execute("DELETE FROM characters WHERE character_id = %s", (character_id,))
        conn.commit()
        conn.close()


async def test_report_export_and_audit_alias_on_postgres(env):
    # 応答の報告は記録・書き出しでき、アカウントを削除すると、監査ログの user_id は別の値に置き換わる
    ops, stores, _, _, database, user_id = env
    started = datetime.now(UTC)
    await stores.set_relationship(user_id, CHARACTER, 1.0, "test", started)
    done = await ops.report(user_id, CHARACTER, "inappropriate", "気になる返事だった", uuid.uuid4(), 2)
    assert done["reported"]
    exported = (await ops.export(user_id))["characters"][CHARACTER]
    assert [(r["reason"], r["note"], r["turn_no"]) for r in exported["reports"]] == [("inappropriate", "気になる返事だった", 2)]
    assert exported["security_events"] == []

    deleted = await ops.delete(user_id)
    assert deleted["deleted"] and deleted["counts"]["account"] == 1
    async with database.pool.connection() as conn:
        await conn.execute("RESET ROLE")
        try:
            count = "SELECT count(*) FROM {} WHERE {} = %s"
            assert await (await conn.execute(count.format("audit_logs", "target_user_id"), (user_id,))).fetchone() == (0,)
            assert await (await conn.execute(count.format("audit_logs", "actor_id"), (str(user_id),))).fetchone() == (0,)
            assert await (await conn.execute(count.format("deletion_requests", "user_id"), (user_id,))).fetchone() == (0,)
            assert await (await conn.execute(count.format("response_reports", "user_id"), (user_id,))).fetchone() == (0,)
            row = await (
                await conn.execute(
                    "SELECT target_user_id, actor_id FROM audit_logs WHERE action = 'account_deleted' AND ts >= %s", (started,)
                )
            ).fetchone()
            assert row is not None and str(row[0]) == row[1] and row[0] != user_id  # 同じ人の行は、同じ別の値
            # このテストが残した行を消す(置き換え後は、user_id では探せないため)
            await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s", (row[0],))
            await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (row[0],))
        finally:
            await conn.execute("SET ROLE mycompanion_app")
    # 保持期間を過ぎた行を消す関数が、アプリのロールで動く(ここでは、消す対象のない、十分に古い日時を指定する)
    assert await ops.purge_audit_logs(datetime(2001, 1, 1, tzinfo=UTC), 1) == 0


async def test_identity_deletion_is_pending_after_account_deletion(env):
    # 外部テナントのアカウントの削除は、保留中として残り、完了にするとオブジェクト ID が記録から消える(0015)
    ops, _, _, _, database, _ = env
    subject = str(uuid.uuid4())
    user_id = await database.resolve_user("entra_external_id", subject)
    started = datetime.now(UTC)
    deleted = await ops.delete(user_id)
    assert deleted["counts"]["account"] == 1
    try:
        assert subject in await ops.pending_identity_deletions()
        assert await ops.complete_identity_deletion(subject, datetime.now(UTC)) == 1
        assert subject not in await ops.pending_identity_deletions()
        assert await ops.complete_identity_deletion(subject, datetime.now(UTC)) == 0  # 2回目は何もしない
    finally:
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            rows = await (
                await conn.execute(
                    "SELECT user_id, status, details_json ? 'idp_subject' FROM deletion_requests "
                    "WHERE scope = 'identity' AND requested_at >= %s AND details_json->>'idp' = 'entra_external_id'",
                    (started,),
                )
            ).fetchall()
            for row in rows:
                await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s", (row[0],))
                await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (row[0],))
            await conn.execute("SET ROLE mycompanion_app")
    assert [(r[1], r[2]) for r in rows] == [("completed", False)]


async def test_ratings_upsert_export_summary_and_delete_on_postgres(env):
    # 応答への評価(0016): 同じターンは上書き、書き出しに入り、集計は件数と平均だけ、削除で消える
    ops, stores, _, _, database, user_id = env
    started = datetime.now(UTC)
    await stores.set_relationship(user_id, CHARACTER, 1.0, "test", started)
    session = uuid.uuid4()
    context = {"character_version": 1, "response_mode": "full", "compose_mode": "single",
               "reply_model": "live-test-model", "reply_prompt_version": "draft-v5+single-v1"}  # fmt: skip
    first = await ops.rate(user_id, CHARACTER, session, 2, {"overall": 2}, "最初", context)
    again = await ops.rate(user_id, CHARACTER, session, 2, {"overall": 5, "empathy": 4}, "付け直し", context)
    assert first["id"] == again["id"]
    (exported,) = (await ops.export(user_id))["characters"][CHARACTER]["ratings"]
    assert (exported["overall"], exported["empathy"], exported["note"]) == (5, 4, "付け直し")
    rows = [r for r in await ops.rating_summary(started) if r["reply_model"] == "live-test-model"]
    assert len(rows) == 1 and rows[0]["n"] == 1 and rows[0]["overall"] == 5.0
    assert str(user_id) not in repr(rows) and "付け直し" not in repr(rows)
    # 別のユーザーからは、見えない(行レベルセキュリティ)
    async with database.scoped_transaction(Scope(uuid.uuid4(), CHARACTER)) as conn:
        assert await (await conn.execute("SELECT count(*) FROM response_ratings")).fetchone() == (0,)
    # 集計を保存する(0017)。評価者の削除のあとも、保存した集計は残る
    label = f"live-test-{uuid.uuid4()}"
    await ops.save_rating_snapshot(label, started, min_n=1)
    deleted = await ops.delete(user_id)
    assert deleted["counts"]["account"] == 1
    assert [r for r in await ops.rating_summary(started) if r["reply_model"] == "live-test-model"] == []
    (snapshot,) = [s for s in await ops.list_rating_snapshots() if s["label"] == label]
    (kept,) = [r for r in snapshot["rows"] if r["reply_model"] == "live-test-model"]
    assert kept["n"] == 1 and kept["overall"] == 5.0 and str(user_id) not in repr(snapshot)
    # このテストが残した削除の記録・監査ログを消す
    async with database.pool.connection() as conn:
        await conn.execute("RESET ROLE")
        await conn.execute("DELETE FROM rating_snapshots WHERE label = %s", (label,))
        row = await (
            await conn.execute(
                "SELECT target_user_id FROM audit_logs WHERE action = 'account_deleted' AND ts >= %s", (started,)
            )
        ).fetchone()
        if row:
            await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s", (row[0],))
            await conn.execute("DELETE FROM deletion_requests WHERE user_id = %s", (row[0],))
        await conn.execute("SET ROLE mycompanion_app")
