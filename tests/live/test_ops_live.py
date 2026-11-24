"""実際の Azure(PostgreSQL・Cosmos DB)で、運営者用の関数とガードレールの記録の読み出しを確かめる(M9 の手順2)。

`.env` を読み込み、`az login` 済みで、マイグレーション 0021 を適用済みであること。既定では実行しない。

    uv run --env-file .env pytest -m azure tests/live/test_ops_live.py
"""

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from mycompanion.account.store import PgAccountStore
from mycompanion.config import CosmosConfig, EngineSettings, PostgresConfig
from mycompanion.db.cosmos import open_cosmos
from mycompanion.db.memory_store import PgMemoryStore
from mycompanion.db.ops_store import PgOperatorAuditLog, PgOperatorStore
from mycompanion.db.postgres import Database, Scope
from mycompanion.ops import views
from mycompanion.ops.audit import ALLOWED, OperatorAuditEntry

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
    account = PgAccountStore(database, cosmos, PgMemoryStore(database, settings))
    user_id = await database.resolve_user("test", f"ops-live-{uuid.uuid4()}")
    actor = f"ops-live-{uuid.uuid4()}"
    try:
        yield database, cosmos, account, PgOperatorStore(database, cosmos), user_id, actor
    finally:
        for store in cosmos.all():
            await store.delete_all_for_user(user_id)
        async with database.pool.connection() as conn:
            await conn.execute("RESET ROLE")
            await conn.execute("DELETE FROM audit_logs WHERE target_user_id = %s OR actor_id = %s", (user_id, actor))
            await conn.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
            await conn.execute("SET ROLE mycompanion_app")
        await cosmos_handle.close()
        await database.close()


async def test_reports_guardrails_and_audit_logs_on_azure(env):
    database, cosmos, account, ops, user_id, actor = env
    now = datetime.now(UTC)
    session_id = uuid.uuid4()
    # 報告の対象のターン(本文を含む)と、そのターンの検出
    await cosmos.turns.put(user_id, CHARACTER, {
        "id": "ops-live-t1", "sessionId": str(session_id), "turnNo": 1, "ts": now.isoformat(),
        "user_input": "秘密の入力", "reply_text": "応答の本文", "response_mode": "brief", "safety_flags": ["x"],
    })  # fmt: skip
    await cosmos.security_events.put(user_id, CHARACTER, {
        "sessionId": str(session_id), "turn_id": "ops-live-t1", "ts": now.isoformat(), "layer": "output",
        "category": "violence", "severity": "low", "detector": "rule", "score": 0.5, "action": "annotate",
        "details_json": {"matched": "根拠"},
    })  # fmt: skip
    report_id = uuid.UUID(
        await account.add_report(
            user_id, CHARACTER, {"session_id": session_id, "turn_no": 1, "reason": "harmful", "note": "メモ"}, now
        )
    )

    # 一覧・詳細(関数から読める。一覧にはメモの本文がない)
    listed = [r for r in await ops.list_reports("open", None, 200) if r["report_id"] == report_id]
    assert len(listed) == 1 and listed[0]["has_note"] is True and "note" not in listed[0]
    row = await ops.get_report(report_id)
    assert row and row["user_id"] == user_id and row["status"] == "open"
    turn, findings = await ops.turn_record(user_id, CHARACTER, session_id, 1)
    detail = views.report_detail(row, turn, findings)
    assert detail["guardrails"]["response_mode"] == "brief"
    assert detail["guardrails"]["screening"][0]["category"] == "violence"
    text = str(detail)
    assert "秘密の入力" not in text and "応答の本文" not in text and "根拠" not in text and str(user_id) not in text

    # 更新(関数だけで。アプリのロールは、報告の表を直接は更新できない)
    assert await ops.update_report(report_id, "resolved", "確認済み", actor, now)
    row = await ops.get_report(report_id)
    assert row and (row["status"], row["ops_memo"], row["ops_updated_by"]) == ("resolved", "確認済み", actor)
    assert not await ops.update_report(uuid.uuid4(), "resolved", None, actor, now)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with database.scoped_transaction(Scope(user_id, CHARACTER)) as conn:
            await conn.execute("UPDATE response_reports SET status = 'open' WHERE report_id = %s", (report_id,))

    # 監査ログ: 運営者の記録を書き、閲覧の関数で読める
    await PgOperatorAuditLog(database).record(
        OperatorAuditEntry(ts=now, actor_id=actor, action="GET /ops/v1/me", decision=ALLOWED, roles=("admin",))
    )
    logs = await ops.list_audit_logs(now - timedelta(minutes=1), None, "operator", None, 500)
    mine = [r for r in logs if r["actor_id"] == actor]
    assert len(mine) == 1 and mine[0]["details_json"]["decision"] == ALLOWED

    # 評価の集計の関数も、運営者の口から呼べる
    assert isinstance(await ops.rating_summary(now - timedelta(days=1)), list)
